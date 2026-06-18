"""
===============================================================================
FILE: services/mock_provider.py
AUTHOR: Monica Peters
CREATED: 2026-06-18

PURPOSE:
In-memory DataProvider for local development and demo without Matthew's backend.

OPERATIONAL:
- Loads fixtures from mock_data.py
- Does not import pipeline/ or eval/
===============================================================================
"""

from __future__ import annotations

from models.candidate import Candidate, CandidateState, next_promotion_state
from mock_data import get_mock_candidate_dicts


class MockDataProvider:
    """Local fixture-backed provider — zero backend required."""

    def __init__(self) -> None:
        self._candidates: dict[str, Candidate] = {
            c.id: c for c in (Candidate.from_mapping(row) for row in get_mock_candidate_dicts())
        }

    def list_candidates(self, state: CandidateState | None = None) -> list[Candidate]:
        items = list(self._candidates.values())
        if state is not None:
            items = [c for c in items if c.state == state]
        return sorted(items, key=lambda c: c.created_at, reverse=True)

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        return self._candidates.get(candidate_id)

    def promote(self, candidate_id: str) -> Candidate:
        candidate = self._require_candidate(candidate_id)
        next_state = next_promotion_state(candidate.state)
        if next_state is None:
            raise ValueError(f"Candidate {candidate_id!r} is already {candidate.state.value}")
        updated = Candidate(
            id=candidate.id,
            title=candidate.title,
            content=candidate.content,
            state=next_state,
            confidence=candidate.confidence,
            provenance=candidate.provenance,
            created_at=candidate.created_at,
            confidence_breakdown=candidate.confidence_breakdown,
            contradiction_ids=list(candidate.contradiction_ids),
        )
        self._candidates[candidate_id] = updated
        return updated

    def reject(self, candidate_id: str, reason: str | None = None) -> None:
        if candidate_id not in self._candidates:
            raise KeyError(f"Unknown candidate id: {candidate_id!r}")
        del self._candidates[candidate_id]

    def _require_candidate(self, candidate_id: str) -> Candidate:
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise KeyError(f"Unknown candidate id: {candidate_id!r}")
        return candidate
