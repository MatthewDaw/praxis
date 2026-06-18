"""
===============================================================================
FILE: models/candidate.py
AUTHOR: Monica Peters
CREATED: 2026-06-18

PURPOSE:
Typed candidate models aligned with the Matthew ↔ Monica API contract.
Python field names use snake_case; JSON/API uses camelCase at the HTTP boundary.

SECURITY:
- Display-only types; no persistence or pipeline logic in this module.

OPERATIONAL:
- Shared by MockDataProvider and ApiDataProvider — UI framework agnostic.
===============================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class CandidateState(str, Enum):
    """Human-gate lifecycle states (contract: proposed | suggested | active | decayed)."""

    PROPOSED = "proposed"
    SUGGESTED = "suggested"
    ACTIVE = "active"
    DECAYED = "decayed"


@dataclass(frozen=True)
class ConfidenceBreakdown:
    """Frequency / recency / breadth decomposition (Day 5+ contract field)."""

    frequency: float = 0.0
    recency: float = 0.0
    breadth: float = 0.0
    frequency_rationale: str = ""
    recency_rationale: str = ""
    breadth_rationale: str = ""


@dataclass
class Candidate:
    """Knowledge candidate shown in the human-gate dashboard."""

    id: str
    title: str
    content: str
    state: CandidateState
    confidence: float
    provenance: str
    created_at: str
    confidence_breakdown: ConfidenceBreakdown | None = None
    contradiction_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Candidate:
        """Build from mock rows or API JSON (camelCase or snake_case keys)."""
        raw_state = data.get("state", CandidateState.PROPOSED.value)
        state = CandidateState(raw_state) if isinstance(raw_state, str) else raw_state

        breakdown_raw = data.get("confidenceBreakdown") or data.get("confidence_breakdown")
        breakdown: ConfidenceBreakdown | None = None
        if isinstance(breakdown_raw, Mapping):
            breakdown = ConfidenceBreakdown(
                frequency=float(breakdown_raw.get("frequency", 0.0)),
                recency=float(breakdown_raw.get("recency", 0.0)),
                breadth=float(breakdown_raw.get("breadth", 0.0)),
                frequency_rationale=str(breakdown_raw.get("frequencyRationale", breakdown_raw.get("frequency_rationale", ""))),
                recency_rationale=str(breakdown_raw.get("recencyRationale", breakdown_raw.get("recency_rationale", ""))),
                breadth_rationale=str(breakdown_raw.get("breadthRationale", breakdown_raw.get("breadth_rationale", ""))),
            )

        contradictions = data.get("contradictions") or data.get("contradiction_ids") or []

        return cls(
            id=str(data["id"]),
            title=str(data["title"]),
            content=str(data["content"]),
            state=state,
            confidence=float(data["confidence"]),
            provenance=str(data["provenance"]),
            created_at=str(data.get("createdAt") or data.get("created_at", "")),
            confidence_breakdown=breakdown,
            contradiction_ids=[str(item) for item in contradictions],
        )

    def to_api_dict(self) -> dict[str, Any]:
        """Serialize for Matthew's API (camelCase)."""
        payload: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "state": self.state.value,
            "confidence": self.confidence,
            "provenance": self.provenance,
            "createdAt": self.created_at,
            "contradictions": self.contradiction_ids,
        }
        if self.confidence_breakdown is not None:
            payload["confidenceBreakdown"] = {
                "frequency": self.confidence_breakdown.frequency,
                "recency": self.confidence_breakdown.recency,
                "breadth": self.confidence_breakdown.breadth,
                "frequencyRationale": self.confidence_breakdown.frequency_rationale,
                "recencyRationale": self.confidence_breakdown.recency_rationale,
                "breadthRationale": self.confidence_breakdown.breadth_rationale,
            }
        return payload


def candidate_state_color(state: CandidateState) -> str:
    """Streamlit markdown color token for lifecycle badges."""
    match state:
        case CandidateState.PROPOSED:
            return "orange"
        case CandidateState.SUGGESTED:
            return "blue"
        case CandidateState.ACTIVE:
            return "green"
        case CandidateState.DECAYED:
            return "gray"
        case _:
            raise ValueError(f"Unhandled candidate state: {state!r}")


def next_promotion_state(current: CandidateState) -> CandidateState | None:
    """Return the next human-gate state, or None if already terminal for promotion."""
    match current:
        case CandidateState.PROPOSED:
            return CandidateState.SUGGESTED
        case CandidateState.SUGGESTED:
            return CandidateState.ACTIVE
        case CandidateState.ACTIVE | CandidateState.DECAYED:
            return None
        case _:
            raise ValueError(f"Unhandled candidate state: {current!r}")
