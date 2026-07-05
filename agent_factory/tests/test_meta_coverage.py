"""Meta-eval: every shipped gate rule is exercised by at least one case (SC2, AE1).

The suite can stay green while a rule rots untested; this meta-test is the guard. It
asserts the coverage matrix has no holes for the shipped rules, and characterizes the
failure modes coverage must catch: an unexercised rule (AE1) and a case tagging a rule-ID
no gate defines.
"""

import pytest

from agent_factory.plan_gate import R_ACCEPT_BINARY, R_NO_DANGLING, R_NO_VAGUE

from evals import coverage
from evals.case_def import EvalCase

PHANTOM_RULE = "R-PHANTOM"  # a rule no case exercises, injected to drive the red path


def test_all_shipped_rules_are_covered():
    # Green-lock: the three backfilled rule_ids cover every shipped rule.
    matrix = coverage.build_matrix()
    holes = matrix.uncovered_rules()
    assert holes == [], f"uncovered shipped rules: {holes}\n{matrix.render()}"


def test_each_shipped_rule_has_an_exercising_case():
    matrix = coverage.build_matrix()
    for rule in coverage.shipped_rule_ids():
        assert matrix.is_covered(rule), f"{rule} has no exercising case"


def test_unexercised_rule_is_reported_and_fails_meta_test():
    # Covers AE1: add a fourth rule constant with no case -> uncovered_rules() returns
    # it and the green-lock assertion fails naming the rule-ID.
    rules = coverage.shipped_rule_ids() + (PHANTOM_RULE,)
    matrix = coverage.build_matrix(rules=rules)

    assert PHANTOM_RULE in matrix.uncovered_rules()

    # The green-lock meta-test would now fail, and its message names the offending rule.
    with pytest.raises(AssertionError) as excinfo:
        holes = matrix.uncovered_rules()
        assert not holes, f"uncovered shipped rules: {holes}"
    assert PHANTOM_RULE in str(excinfo.value)


def test_case_tagging_nonexistent_rule_is_flagged():
    # Edge: a case declaring a rule-ID that no gate defines must be flagged as dangling,
    # so it cannot claim phantom coverage.
    bogus = EvalCase(
        id="bogus_tag_case",
        component="plan_gate",
        rule_ids=["R-DOES-NOT-EXIST"],
    )
    matrix = coverage.build_matrix(cases=[bogus])
    dangling = matrix.dangling_tags()
    assert "bogus_tag_case" in dangling
    assert dangling["bogus_tag_case"] == ["R-DOES-NOT-EXIST"]


def test_render_distinguishes_holes_from_covered_cells():
    rules = coverage.shipped_rule_ids() + (PHANTOM_RULE,)
    rendered = coverage.build_matrix(rules=rules).render()
    assert coverage.COVERED_CELL in rendered
    assert coverage.HOLE_CELL in rendered
    assert PHANTOM_RULE in rendered  # the hole is named in the render


def test_rules_without_red_case_uses_field_presence():
    # FIELD PRESENCE only: a case with any red_proof dict counts; one without does not.
    with_red = EvalCase(
        id="has_red",
        component="plan_gate",
        rule_ids=[R_ACCEPT_BINARY],
        red_proof={"kind": "fixture", "ref": "broken-accept"},
    )
    without_red = EvalCase(
        id="no_red",
        component="plan_gate",
        rule_ids=[R_NO_VAGUE],
    )
    matrix = coverage.build_matrix(
        rules=(R_ACCEPT_BINARY, R_NO_VAGUE, R_NO_DANGLING),
        cases=[with_red, without_red],
    )
    without = matrix.rules_without_red_case()
    assert R_ACCEPT_BINARY not in without  # has a case with red_proof present
    assert R_NO_VAGUE in without            # exercised, but case lacks red_proof
    assert R_NO_DANGLING in without         # no exercising case at all


def test_proposed_cases_do_not_count_toward_coverage():
    proposed = EvalCase(
        id="proposed_case",
        component="plan_gate",
        rule_ids=[R_ACCEPT_BINARY],
        status="proposed",
    )
    matrix = coverage.build_matrix(rules=(R_ACCEPT_BINARY,), cases=[proposed])
    assert R_ACCEPT_BINARY in matrix.uncovered_rules()
