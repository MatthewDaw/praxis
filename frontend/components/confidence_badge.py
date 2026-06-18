"""Confidence and lifecycle badge components."""

from __future__ import annotations

import streamlit as st

from models.candidate import Candidate, CandidateState, candidate_state_color


def render_state_badge(state_label: str, state: CandidateState | None = None) -> None:
    """Color-coded lifecycle badge (proposed / suggested / active / decayed / unknown)."""
    enum_state = state if state is not None else CandidateState(state_label)
    color = candidate_state_color(enum_state)
    st.markdown(f":{color}[**{state_label.upper()}**]")


def render_confidence_progress(confidence: float) -> None:
    """Aggregate confidence score as a progress bar."""
    st.progress(
        confidence,
        text=f"Confidence: {confidence:.2f}",
    )


def render_confidence_breakdown(candidate: Candidate) -> None:
    """
    Frequency / recency / breadth breakdown with rationale tooltips (Day 3+).

    Uses placeholder decomposition from aggregate score when breakdown is absent.
    """
    breakdown = candidate.confidence_breakdown
    if breakdown is None:
        st.caption("Detailed breakdown pending pipeline scoring (Matthew, Day 5).")
        render_confidence_progress(candidate.confidence)
        return

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric(
            "Frequency",
            f"{breakdown.frequency:.0%}",
            help=breakdown.frequency_rationale
            or "How often this lesson appeared across sessions — higher means repeated evidence.",
        )
    with c2:
        st.metric(
            "Recency",
            f"{breakdown.recency:.0%}",
            help=breakdown.recency_rationale
            or "How recently this pattern was observed — higher means fresher signal.",
        )
    with c3:
        st.metric(
            "Breadth",
            f"{breakdown.breadth:.0%}",
            help=breakdown.breadth_rationale
            or "How many distinct contexts support this lesson — higher means broader applicability.",
        )
    render_confidence_progress(candidate.confidence)
