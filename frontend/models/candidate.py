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

# Keys consumed by from_mapping; any other API fields are preserved in Candidate.extra.
_KNOWN_MAPPING_KEYS = frozenset(
    {
        "id",
        "title",
        "content",
        "state",
        "confidence",
        "provenance",
        "source",
        "source_log",
        "sourceLog",
        "createdAt",
        "created_at",
        "updatedAt",
        "updated_at",
        "confidenceBreakdown",
        "confidence_breakdown",
        "contradictions",
        "contradiction_ids",
    }
)


class CandidateState(str, Enum):
    """Human-gate lifecycle states (contract: proposed | active | decayed)."""

    PROPOSED = "proposed"
    ACTIVE = "active"
    DECAYED = "decayed"
    UNRECOGNIZED = "unrecognized"


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
    state_label: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def display_state(self) -> str:
        """Lifecycle label for UI — preserves teammate API values when enum is unknown."""
        if self.state is CandidateState.UNRECOGNIZED:
            return self.state_label or self.state.value
        return self.state.value

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Candidate:
        """
        Build from mock rows or pipeline API JSON.

        Accepts camelCase or snake_case keys, optional fields with defaults, and
        preserves unknown top-level fields in ``extra`` so Matthew/Dominic can extend
        the contract without breaking the dashboard.
        """
        raw_state = data.get("state", CandidateState.PROPOSED.value)
        state_label = raw_state.value if isinstance(raw_state, CandidateState) else str(raw_state)
        state = _parse_state(raw_state)

        breakdown_raw = data.get("confidenceBreakdown") or data.get("confidence_breakdown")
        breakdown: ConfidenceBreakdown | None = None
        if isinstance(breakdown_raw, Mapping):
            breakdown = ConfidenceBreakdown(
                frequency=float(breakdown_raw.get("frequency", 0.0)),
                recency=float(breakdown_raw.get("recency", 0.0)),
                breadth=float(breakdown_raw.get("breadth", 0.0)),
                frequency_rationale=str(
                    breakdown_raw.get(
                        "frequencyRationale",
                        breakdown_raw.get("frequency_rationale", ""),
                    )
                ),
                recency_rationale=str(
                    breakdown_raw.get(
                        "recencyRationale",
                        breakdown_raw.get("recency_rationale", ""),
                    )
                ),
                breadth_rationale=str(
                    breakdown_raw.get(
                        "breadthRationale",
                        breakdown_raw.get("breadth_rationale", ""),
                    )
                ),
            )

        contradictions = data.get("contradictions") or data.get("contradiction_ids") or []
        contradiction_ids = _normalize_contradiction_ids(contradictions)

        provenance = _first_str(
            data,
            "provenance",
            "source",
            "source_log",
            "sourceLog",
            default="",
        )
        created_at = _first_str(
            data,
            "createdAt",
            "created_at",
            "updatedAt",
            "updated_at",
            default="",
        )

        extra = {key: value for key, value in data.items() if key not in _KNOWN_MAPPING_KEYS}

        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            content=str(data.get("content", "")),
            state=state,
            confidence=float(data.get("confidence", 0.0)),
            provenance=provenance,
            created_at=created_at,
            confidence_breakdown=breakdown,
            contradiction_ids=contradiction_ids,
            state_label=state_label,
            extra=extra,
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
        if self.extra:
            payload.update(self.extra)
        return payload


def _first_str(data: Mapping[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return default


def _parse_state(raw: Any) -> CandidateState:
    if isinstance(raw, CandidateState):
        return raw
    try:
        return CandidateState(str(raw))
    except ValueError:
        return CandidateState.UNRECOGNIZED


def _normalize_contradiction_ids(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    ids: list[str] = []
    for item in raw:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, Mapping) and item.get("id") is not None:
            ids.append(str(item["id"]))
    return ids


def candidate_state_style(state: CandidateState) -> dict[str, str]:
    """Enterprise muted pill colors shared with frontend-react tokens."""
    match state:
        case CandidateState.PROPOSED:
            return {"bg": "#fef3c7", "text": "#92400e", "border": "#fcd34d"}
        case CandidateState.ACTIVE:
            return {"bg": "#dcfce7", "text": "#166534", "border": "#86efac"}
        case CandidateState.DECAYED | CandidateState.UNRECOGNIZED:
            return {"bg": "#f3f4f6", "text": "#4b5563", "border": "#d1d5db"}
        case _:
            raise ValueError(f"Unhandled candidate state: {state!r}")


def candidate_state_color(state: CandidateState) -> str:
    """Legacy helper — returns text color hex for badges."""
    return candidate_state_style(state)["text"]


def next_promotion_state(current: CandidateState) -> CandidateState | None:
    """Return the next human-gate state, or None if already terminal for promotion."""
    match current:
        case CandidateState.PROPOSED:
            return CandidateState.ACTIVE
        case CandidateState.ACTIVE | CandidateState.DECAYED | CandidateState.UNRECOGNIZED:
            return None
        case _:
            raise ValueError(f"Unhandled candidate state: {current!r}")
