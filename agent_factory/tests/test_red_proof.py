"""Tests for the RED-proof leaf (plan unit U3; R7, AE2).

Each test constructs a synthetic ``EvalCase`` in-memory (no on-disk ``case.yaml``)
so the leaf is self-contained: a reject case that exercises only ``R-NO-DANGLING``,
varied by what RED-proof it declares.
"""

from agent_factory.plan_gate import R_NO_DANGLING, R_NO_VAGUE, PlanGate
from evals.case_def import CheckRef, EvalCase
from evals.red_proof import (
    BROKEN_GATES,
    audit_red_proofs,
    decorative_results,
    red_proven_case_ids,
    verify_red_proof,
)


def _dangling_reject_case(case_id: str, red_proof: dict | None) -> EvalCase:
    """A reject case whose only violation is a dangling concept reference.

    The real gate rejects it (so it is green in the normal suite); a broken gate that
    disables ``R-NO-DANGLING`` wrongly admits it.
    """
    return EvalCase(
        id=case_id,
        component="plan_gate",
        input={
            "requirements": [
                {
                    "id": "R1",
                    "text": "The team streak advances when athletes log activity.",
                    "acceptance": "streak += 1 on any day with a completed submission.",
                    "defines": [],
                    "references": ["ghost concept"],
                    # valid source so R-HAS-SOURCE passes; the only firing rule stays R-NO-DANGLING
                    "source": "prd-team-app",
                },
            ],
            "out_of_scope": [],
        },
        deterministic_checks=[
            CheckRef(
                name="gate_rejects",
                ref="evals.checks:gate_rejects",
                params={"reason_contains": "ghost concept"},
            ),
        ],
        rule_ids=[R_NO_DANGLING],
        red_proof=red_proof,
    )


def test_reject_case_goes_red_against_broken_fixture_is_verified():
    case = _dangling_reject_case("rp_verified", {"fixture": R_NO_DANGLING})

    # Sanity: the real gate REJECTS this input (the case is green in the live suite).
    assert PlanGate().evaluate(case.input).admitted is False
    # The broken fixture WRONGLY ADMITS it (the deliberate one-rule mutant).
    assert BROKEN_GATES[R_NO_DANGLING].evaluate(case.input).admitted is True

    result = verify_red_proof(case)
    assert result.verified
    assert result.fixture == R_NO_DANGLING
    assert case.id in red_proven_case_ids([case])


def test_case_without_red_proof_is_decorative_and_reported():
    # Covers AE2: a case with no red_proof is quarantined and surfaced.
    case = _dangling_reject_case("rp_decorative", None)

    result = verify_red_proof(case)
    assert result.decorative
    assert not result.verified

    reported = {r.case_id for r in decorative_results([case])}
    assert case.id in reported
    # A decorative case cannot prop up a rule's RED-coverage claim.
    assert case.id not in red_proven_case_ids([case])


def test_fixture_that_does_not_flip_verdict_is_flagged_bogus():
    # Names the WRONG fixture: disabling R-NO-VAGUE leaves the dangling reason firing,
    # so the case still rejects (passes) against the broken gate -> non-falsifying.
    case = _dangling_reject_case("rp_bogus", {"fixture": R_NO_VAGUE})

    # The broken R-NO-VAGUE gate still REJECTS this dangling input.
    assert BROKEN_GATES[R_NO_VAGUE].evaluate(case.input).admitted is False

    result = verify_red_proof(case)
    assert result.bogus
    assert not result.verified
    assert case.id not in red_proven_case_ids([case])


def test_audit_classifies_a_mixed_suite():
    cases = [
        _dangling_reject_case("rp_verified", {"fixture": R_NO_DANGLING}),
        _dangling_reject_case("rp_decorative", None),
        _dangling_reject_case("rp_bogus", {"fixture": R_NO_VAGUE}),
    ]
    by_id = {r.case_id: r.status for r in audit_red_proofs(cases)}
    assert by_id == {
        "rp_verified": "verified",
        "rp_decorative": "decorative",
        "rp_bogus": "bogus",
    }
