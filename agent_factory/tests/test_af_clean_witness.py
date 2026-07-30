"""R13 acceptance: af-clean's witness-tiered applier gate (B22, B12/D2).

Acceptance (verbatim from the ticket): a proposal with no witness is downgraded to report and
never applied; one rule yields an auto-appliable lexical instance and a report-only semantic
instance; blame-flagged defensive code is refused; and the aggression dial is PINNED at its
default witness tier with no lowering mechanism in v1, so a corpus-present symbol and an unbound
test deletion are refused unconditionally.
"""

from __future__ import annotations

from agent_factory.af_clean_witness import (
    DEFAULT_REQUIRED_WITNESS_TIER,
    TIER_AST_CORPUS_EXECUTION,
    TIER_NAMED_TOOL,
    TIER_SURVIVED_TOMBSTONE,
    Proposal,
    decide,
)


def _base_proposal(**overrides) -> Proposal:
    defaults = dict(
        rule="dead-symbol-removal",
        tier_ceiling="enforce",
        match_kind=None,
        witness_tier=TIER_SURVIVED_TOMBSTONE,
    )
    defaults.update(overrides)
    return Proposal(**defaults)


def test_default_witness_tier_is_the_hard_floor():
    # B22: the dial may never go below tier 2 (AST no-reference + corpus absence + execution
    # evidence). The default IS the floor, in v1 there is no lowering mechanism.
    assert DEFAULT_REQUIRED_WITNESS_TIER == TIER_AST_CORPUS_EXECUTION


def test_proposal_with_no_witness_is_downgraded_to_report_never_applied():
    proposal = _base_proposal(witness_tier=None)
    decision = decide(proposal)
    assert decision.action == "report"
    assert "witness" in decision.reason


def test_proposal_with_witness_below_required_tier_is_downgraded_to_report():
    proposal = _base_proposal(witness_tier=TIER_NAMED_TOOL)  # tier 1 < default tier 2
    decision = decide(proposal)
    assert decision.action == "report"


def test_proposal_with_witness_at_or_above_required_tier_may_apply():
    proposal = _base_proposal(witness_tier=TIER_AST_CORPUS_EXECUTION)
    decision = decide(proposal)
    assert decision.action == "apply"


def test_same_rule_lexical_instance_auto_appliable_semantic_instance_report_only():
    lexical = _base_proposal(rule="same-job-consolidation", match_kind="lexical",
                             witness_tier=TIER_AST_CORPUS_EXECUTION)
    semantic = _base_proposal(rule="same-job-consolidation", match_kind="semantic",
                              witness_tier=TIER_AST_CORPUS_EXECUTION)
    assert decide(lexical).action == "apply"
    semantic_decision = decide(semantic)
    assert semantic_decision.action == "report"
    assert "semantic" in semantic_decision.reason


def test_advise_tier_rule_never_applies_regardless_of_witness():
    proposal = _base_proposal(tier_ceiling="advise", witness_tier=TIER_SURVIVED_TOMBSTONE)
    decision = decide(proposal)
    assert decision.action == "report"


def test_blame_flagged_defensive_code_is_refused_at_every_dial_setting():
    proposal = _base_proposal(is_scar=True, witness_tier=TIER_SURVIVED_TOMBSTONE)
    decision = decide(proposal)
    assert decision.action == "report"
    assert "scar" in decision.reason or "blame" in decision.reason


def test_corpus_present_symbol_is_refused_unconditionally():
    proposal = _base_proposal(corpus_present=True, witness_tier=TIER_SURVIVED_TOMBSTONE)
    decision = decide(proposal)
    assert decision.action == "report"
    assert "corpus" in decision.reason


def test_unbound_test_deletion_is_refused_unconditionally():
    proposal = _base_proposal(
        is_test_deletion=True, bound_to_symbol_deletion=False,
        witness_tier=TIER_SURVIVED_TOMBSTONE,
    )
    decision = decide(proposal)
    assert decision.action == "report"
    assert "test deletion" in decision.reason


def test_bound_test_deletion_with_sufficient_witness_may_apply():
    proposal = _base_proposal(
        is_test_deletion=True, bound_to_symbol_deletion=True,
        witness_tier=TIER_SURVIVED_TOMBSTONE,
    )
    decision = decide(proposal)
    assert decision.action == "apply"


def test_decide_accepts_no_required_tier_argument_the_dial_is_pinned():
    # No lowering mechanism in v1: `decide` takes no caller-supplied required-tier argument.
    import inspect

    params = inspect.signature(decide).parameters
    assert "required_witness_tier" not in params
    assert "required_tier" not in params
