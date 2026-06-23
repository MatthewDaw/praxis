"""Smoke and extended tests for mock DataProvider gate workflow."""

from __future__ import annotations

from knowledge.evals.run import load_cases
from models.candidate import CandidateState, next_promotion_state
from services.data_provider import get_data_provider
from services.mock_provider import MockDataProvider


def test_mock_provider_lists_candidates() -> None:
    provider = get_data_provider()
    candidates = provider.list_candidates()
    assert len(candidates) >= len(load_cases())


def test_mock_promote_proposed_to_active() -> None:
    provider = get_data_provider()
    updated = provider.promote("cand_1")
    assert updated.state is CandidateState.ACTIVE


def test_mock_contradiction_pair_exists() -> None:
    provider = get_data_provider()
    primary = provider.get_candidate("cand_9")
    assert primary is not None
    assert "cand_16" in primary.contradiction_ids


def test_next_promotion_state_chain() -> None:
    assert next_promotion_state(CandidateState.PROPOSED) is CandidateState.ACTIVE
    assert next_promotion_state(CandidateState.ACTIVE) is None


def test_mock_reject_decays_candidate() -> None:
    provider = MockDataProvider()
    assert provider.get_candidate("cand_3") is not None
    provider.reject("cand_3", reason="duplicate lesson")
    decayed = provider.get_candidate("cand_3")
    assert decayed is not None
    assert decayed.state is CandidateState.DECAYED


def test_mock_active_is_terminal_for_promotion() -> None:
    provider = MockDataProvider()
    before = provider.get_candidate("cand_2")
    assert before is not None
    assert before.state is CandidateState.ACTIVE
    assert next_promotion_state(before.state) is None


def test_mock_resolve_contradiction_clears_rival() -> None:
    provider = MockDataProvider()
    primary = provider.get_candidate("cand_9")
    assert primary is not None
    assert "cand_16" in primary.contradiction_ids

    updated = provider.resolve_contradiction(
        "cand_9__cand_16",
        resolution="keep_primary",
        keep_id="cand_9",
    )
    assert "cand_16" not in updated.contradiction_ids
    rival = provider.get_candidate("cand_16")
    assert rival is not None
    assert rival.state is CandidateState.DECAYED
    assert provider.get_candidate("cand_9") is not None
