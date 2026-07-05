"""Runs the agent-factory eval suite: discover every cases/<name>/case.yaml,
produce the component's verdict, and assert each deterministic check passes.

Mirrors the Praxis harness shape (parametrized over discovered cases) so new
edge cases are added as data, not as new test code.
"""

from pathlib import Path

import pytest

from evals.case_def import discover_cases
from evals.checks import produce_verdict, resolve_check

CASES_ROOT = Path(__file__).resolve().parent.parent / "evals" / "cases"
# Only ``active`` cases lock the suite. Harvested escapes land as ``status: proposed``
# in evals/cases/_quarantine/ and must NOT green-lock until a human ratifies them
# (a harvested case encodes a plan the gate wrongly admitted — its check would fail
# until the gate is fixed), so they are excluded from the locking run here.
CASES = [c for c in discover_cases(CASES_ROOT) if c.status == "active"]


def _case_check_params():
    for case in CASES:
        verdict = produce_verdict(case.component, case.input)
        for check in case.deterministic_checks:
            yield pytest.param(verdict, check, id=f"{case.id}::{check.name}")


@pytest.mark.parametrize("verdict, check", list(_case_check_params()))
def test_eval_case_check(verdict, check):
    fn = resolve_check(check.ref)
    result = fn(verdict, **check.params)
    assert result.passed, f"{check.name}: {result.evidence}"


def test_cases_were_discovered():
    # Guard against the suite silently passing because no case.yaml was found.
    assert CASES, f"no case.yaml discovered under {CASES_ROOT}"


# --- characterization: the Gate-contract refactor must not change verdicts -----

# Pinned admit/reject decision, fired rule-IDs, and reason message text for each
# existing case. Captured from the pre-refactor string-reason gate so the refactor
# (structured reasons via the registry) is provably non-breaking.
EXPECTED_VERDICTS = {
    "plan_gate_well_formed_admitted": {
        "admitted": True,
        "rule_ids": [],
        "messages": [],
    },
    "plan_gate_missing_acceptance_rejected": {
        "admitted": False,
        "rule_ids": ["R-ACCEPT-BINARY"],
        "messages": ["R5: no binary acceptance condition"],
    },
    "plan_gate_vague_term_rejected": {
        "admitted": False,
        "rule_ids": ["R-NO-VAGUE"],
        "messages": [
            "R9: vague term 'fast' without a measurable threshold",
            "R9: vague term 'most users' without a measurable threshold",
        ],
    },
    "plan_gate_dangling_concept_reference_rejected": {
        "admitted": False,
        "rule_ids": ["R-NO-DANGLING"],
        "messages": [
            "R2: dangling reference to undefined concept 'team streak' "
            "(define it in a requirement or declare it out of scope)",
        ],
    },
    "plan_gate_missing_project_source_rejected": {
        "admitted": False,
        "rule_ids": ["R-HAS-SOURCE"],
        "messages": [
            "R1: missing/!= project source (expected prd-team-app, got 'team-app')",
            "R2: missing/!= project source (expected prd-team-app, got '')",
        ],
    },
    "plan_gate_well_formed_with_deps_admitted": {
        "admitted": True,
        "rule_ids": [],
        "messages": [],
    },
    "plan_gate_dangling_dependency_rejected": {
        "admitted": False,
        "rule_ids": ["R-NO-DANGLING-DEP"],
        "messages": [
            "R2: depends_on 'R7' which is not a requirement in this plan "
            "(add the prerequisite or fix the edge)",
        ],
    },
    "plan_gate_dependency_cycle_rejected": {
        "admitted": False,
        "rule_ids": ["R-NO-DEP-CYCLE"],
        "messages": [
            "dependency cycle: R1 -> R2 -> R1 "
            "(no ticket in the cycle can ever be ready; break it)",
        ],
    },
}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_plan_gate_verdict_unchanged(case):
    expected = EXPECTED_VERDICTS[case.id]
    verdict = produce_verdict(case.component, case.input)
    assert verdict.admitted == expected["admitted"]
    assert verdict.rule_ids == expected["rule_ids"]
    assert [r.message for r in verdict.reasons] == expected["messages"]


def test_all_existing_cases_are_characterized():
    # If a case is added/removed without updating the snapshot, fail loudly.
    assert {c.id for c in CASES} == set(EXPECTED_VERDICTS)
