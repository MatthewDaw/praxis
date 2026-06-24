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

from datetime import datetime, timezone
from typing import Any

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
        audit = _append_audit(
            candidate,
            action=f"promoted_to_{next_state.value}",
            actor="human-gate",
        )
        updated = _clone_candidate(candidate, state=next_state, extra=audit)
        self._candidates[candidate_id] = updated
        return updated

    def reject(self, candidate_id: str, reason: str | None = None) -> None:
        candidate = self._require_candidate(candidate_id)
        audit = _append_audit(
            candidate,
            action="rejected",
            actor="human-gate",
            note=reason or "",
        )
        updated = _clone_candidate(candidate, state=CandidateState.REJECTED, extra=audit)
        self._candidates[candidate_id] = updated

    def resolve_contradiction(
        self,
        contradiction_id: str,
        *,
        resolution: str,
        keep_id: str,
    ) -> Candidate:
        if resolution == "defer":
            raise ValueError("Defer is a UI-only action — no mutation performed.")

        primary_id, rival_id = _parse_contradiction_pair(contradiction_id, keep_id)
        keeper = self._require_candidate(keep_id)
        loser_id = rival_id if keep_id == primary_id else primary_id
        loser = self._candidates.get(loser_id)
        if loser is not None:
            loser_audit = _append_audit(
                loser,
                action="superseded",
                actor="human-gate",
                note=f"lost contradiction to {keep_id}",
            )
            cleared_loser_ids = [
                cid for cid in loser.contradiction_ids if cid != keep_id
            ]
            self._candidates[loser_id] = _clone_candidate(
                loser,
                state=CandidateState.REJECTED,
                contradiction_ids=cleared_loser_ids,
                extra=loser_audit,
            )

        cleared_ids = [cid for cid in keeper.contradiction_ids if cid != loser_id]
        audit = _append_audit(
            keeper,
            action="contradiction_resolved",
            actor="human-gate",
            note=f"Kept {keep_id} over {loser_id} ({resolution})",
        )
        updated = _clone_candidate(keeper, contradiction_ids=cleared_ids, extra=audit)
        self._candidates[keep_id] = updated
        return updated

    def _require_candidate(self, candidate_id: str) -> Candidate:
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise KeyError(f"Unknown candidate id: {candidate_id!r}")
        return candidate


def _parse_contradiction_pair(contradiction_id: str, keep_id: str) -> tuple[str, str]:
    if "__" in contradiction_id:
        left, right = contradiction_id.split("__", 1)
        return left, right
    raise ValueError(f"Invalid contradiction id: {contradiction_id!r}")


def _append_audit(
    candidate: Candidate,
    *,
    action: str,
    actor: str,
    note: str = "",
) -> dict[str, Any]:
    extra = dict(candidate.extra)
    trail = list(extra.get("auditTrail") or extra.get("audit_trail") or [])
    entry: dict[str, Any] = {
        "action": action,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provenance": candidate.provenance,
        "actor": actor,
    }
    if note:
        entry["note"] = note
    trail.append(entry)
    extra["auditTrail"] = trail
    return extra


def _clone_candidate(
    candidate: Candidate,
    *,
    state: CandidateState | None = None,
    contradiction_ids: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> Candidate:
    resolved_state = state if state is not None else candidate.state
    return Candidate(
        id=candidate.id,
        title=candidate.title,
        content=candidate.content,
        state=resolved_state,
        confidence=candidate.confidence,
        provenance=candidate.provenance,
        created_at=candidate.created_at,
        confidence_breakdown=candidate.confidence_breakdown,
        contradiction_ids=list(contradiction_ids if contradiction_ids is not None else candidate.contradiction_ids),
        state_label=resolved_state.value,
        extra=dict(extra if extra is not None else candidate.extra),
    )
