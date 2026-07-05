"""U7 loader guard: the deterministic suite must reject flaky-shaped cases at discovery.

A case whose ``input`` block carries a non-deterministic concern (sleep / timeout /
latency / timestamp / now / concurrency / retries) is refused when loaded, and pointed
at a separate ``stress/`` lane. Matching is key-level with word boundaries, so a benign
key like ``now_admitted`` is never false-flagged.
"""

from pathlib import Path

import pytest

from evals.case_def import EvalCase, discover_cases, load_case

CASES_ROOT = Path(__file__).resolve().parent.parent / "evals" / "cases"

_CHECKS = [{"name": "gate_admits", "ref": "evals.checks:gate_admits"}]


def _case(input_block: dict) -> dict:
    return {
        "id": "guard_probe",
        "component": "plan_gate",
        "input": input_block,
        "deterministic_checks": _CHECKS,
    }


@pytest.mark.parametrize(
    "flaky_key", ["timeout", "latency", "sleep", "timestamp", "now", "concurrency", "retries"]
)
def test_flaky_shaped_case_is_rejected(flaky_key):
    with pytest.raises(ValueError) as exc:
        EvalCase.from_dict(_case({flaky_key: 5, "requirements": []}))
    msg = str(exc.value)
    assert flaky_key in msg
    assert "stress/" in msg


def test_flaky_key_nested_in_input_is_rejected():
    # The scan is recursive: a flaky key buried inside a requirement is still caught.
    nested = {"requirements": [{"id": "R1", "acceptance": "x", "retries": 3}]}
    with pytest.raises(ValueError) as exc:
        EvalCase.from_dict(_case(nested))
    assert "retries" in str(exc.value)


def test_benign_now_admitted_is_not_flagged():
    # Word-boundary match: "now_admitted" must NOT trip the "now" keyword.
    case = EvalCase.from_dict(_case({"now_admitted": True, "requirements": []}))
    assert case.id == "guard_probe"


@pytest.mark.parametrize("benign_key", ["now_admitted", "timeout_policy_doc", "is_active_now_field"])
def test_benign_substring_keys_not_flagged(benign_key):
    # Underscore is a word char, so these compound keys carry no standalone keyword.
    EvalCase.from_dict(_case({benign_key: 1, "requirements": []}))


def test_existing_cases_still_load():
    cases = discover_cases(CASES_ROOT)
    assert cases, "expected the existing deterministic cases to load"
    assert all(c.status == "active" for c in cases)


def test_load_case_round_trip_carries_new_fields(tmp_path):
    yaml_text = (
        "id: rt_probe\n"
        "component: plan_gate\n"
        "status: proposed\n"
        "rule_ids: [R-NO-VAGUE]\n"
        "red_proof:\n"
        "  kind: harvested\n"
        "  event_seq: 7\n"
        "input:\n"
        "  requirements: []\n"
        "deterministic_checks:\n"
        "  - name: gate_admits\n"
        "    ref: evals.checks:gate_admits\n"
    )
    p = tmp_path / "case.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    case = load_case(p)
    assert case.status == "proposed"
    assert case.rule_ids == ["R-NO-VAGUE"]
    assert case.red_proof == {"kind": "harvested", "event_seq": 7}
