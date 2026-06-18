"""Typed domain models for the human-gate dashboard (API contract surface)."""

from models.candidate import (
    Candidate,
    CandidateState,
    ConfidenceBreakdown,
    candidate_state_color,
    next_promotion_state,
)

__all__ = [
    "Candidate",
    "CandidateState",
    "ConfidenceBreakdown",
    "candidate_state_color",
    "next_promotion_state",
]
