"""Unit tests for the two architecture-decision rules in the plan done-gate.

These isolate the D1-D5 dependency-inversion guards that ``evaluate_plan`` enforces once a
ticket is correctly tagged ``architecture-decision``:

- ``R-DECISION-NOT-END-STATE`` — a decision ticket must be ``verify="manual"`` (human-accepted),
  never a machine-built impl end-state.
- ``R-NO-IMPL-DEPENDS-ON-DECISION`` — no ticket may ``depends_on`` a decision (a decision sits
  first in build order but can only go green last, wedging the run).

Imports the gate directly (``pythonpath = ["src", "."]`` from pyproject makes
``agent_factory.plan_gate`` importable, the same pattern tests/test_meta_coverage.py uses).
"""

from agent_factory.plan_gate import (
    R_DECISION_NOT_END_STATE,
    R_NO_IMPL_DEPENDS_ON_DECISION,
    Requirement,
    evaluate_plan,
)


def _decision(verify: str, depends_on=None) -> Requirement:
    return Requirement(
        id="D1",
        text="Cognito is the chosen identity provider for the three user tiers.",
        acceptance="Cognito is the accepted identity-provider design decision for the three tiers.",
        source="prd-sotos",
        depends_on=depends_on or [],
        tags=["architecture-decision"],
        verify=verify,
    )


def _impl(depends_on=None) -> Requirement:
    return Requirement(
        id="R1",
        text="The sign-up handler creates a Cognito user in the requested tier's UserPool.",
        acceptance="POST /signup with a valid tier creates one Cognito user and returns 201.",
        source="prd-sotos",
        depends_on=depends_on or [],
        tags=["auth"],
        verify="automated",
    )


def test_malformed_decision_fires_both_rules():
    # Decision tagged architecture-decision but verify=automated with an impl end-state acceptance,
    # AND an impl ticket depends_on it — the full anti-pattern. Both decision rules must fire.
    decision = _decision(verify="automated", depends_on=[])
    decision.acceptance = "cdk synth emits three UserPools."
    verdict = evaluate_plan([decision, _impl(depends_on=["D1"])], project="sotos")

    assert not verdict.admitted
    assert R_DECISION_NOT_END_STATE in verdict.rule_ids
    assert R_NO_IMPL_DEPENDS_ON_DECISION in verdict.rule_ids


def test_correctly_modeled_decision_is_admitted():
    # verify=manual, decision-level acceptance, and NO ticket depends_on the decision.
    verdict = evaluate_plan([_decision(verify="manual"), _impl(depends_on=[])], project="sotos")

    assert verdict.admitted
    assert verdict.rule_ids == []


def test_decision_not_end_state_fires_alone():
    # verify=automated decision but NOTHING depends_on it: isolates R-DECISION-NOT-END-STATE.
    verdict = evaluate_plan([_decision(verify="automated"), _impl(depends_on=[])], project="sotos")

    assert not verdict.admitted
    assert verdict.rule_ids == [R_DECISION_NOT_END_STATE]


def test_no_impl_depends_on_decision_fires_alone():
    # Decision is otherwise well-formed (verify=manual) but an impl ticket depends_on it:
    # isolates R-NO-IMPL-DEPENDS-ON-DECISION.
    verdict = evaluate_plan([_decision(verify="manual"), _impl(depends_on=["D1"])], project="sotos")

    assert not verdict.admitted
    assert verdict.rule_ids == [R_NO_IMPL_DEPENDS_ON_DECISION]
