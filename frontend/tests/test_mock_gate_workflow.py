"""Smoke tests for mock DataProvider gate workflow (demo rehearsal)."""

from __future__ import annotations

from models.candidate import CandidateState, next_promotion_state
from services.data_provider import get_data_provider


def test_mock_provider_lists_candidates() -> None:
    provider = get_data_provider()
    candidates = provider.list_candidates()
    assert len(candidates) >= 17


def test_mock_promote_proposed_to_suggested() -> None:
    provider = get_data_provider()
    updated = provider.promote("cand_1")
    assert updated.state is CandidateState.SUGGESTED


def test_mock_contradiction_pair_exists() -> None:
    provider = get_data_provider()
    primary = provider.get_candidate("cand_9")
    assert primary is not None
    assert "cand_16" in primary.contradiction_ids


def test_next_promotion_state_chain() -> None:
    assert next_promotion_state(CandidateState.PROPOSED) is CandidateState.SUGGESTED
    assert next_promotion_state(CandidateState.SUGGESTED) is CandidateState.ACTIVE
    assert next_promotion_state(CandidateState.ACTIVE) is None
