"""Confidence and lifecycle badge components."""

from __future__ import annotations

import html

import streamlit as st

from models.candidate import Candidate, CandidateState, candidate_state_style


def render_state_badge(state_label: str, state: CandidateState | None = None) -> None:
    """Color-coded lifecycle badge (proposed / suggested / active / decayed / unknown)."""
    enum_state = state if state is not None else CandidateState(state_label)
    style = candidate_state_style(enum_state)
    safe_label = html.escape(state_label)
    st.markdown(
        f'<span class="state-badge" style="background:{style["bg"]};'
        f'color:{style["text"]};border-color:{style["border"]};">{safe_label}</span>',
        unsafe_allow_html=True,
    )


def render_confidence_progress(confidence: float) -> None:
    """Aggregate confidence score as a progress bar."""
    pct = int(confidence * 100)
    st.markdown(
        f'<div style="background:#e2e8f0;border-radius:999px;height:8px;overflow:hidden;">'
        f'<div style="width:{pct}%;height:100%;background:linear-gradient(90deg,#60a5fa,#2563eb);">'
        f"</div></div>"
        f'<p style="font-size:0.85rem;margin:0.35rem 0 0;color:#6b7280;">'
        f"Aggregate: <strong>{confidence:.2f}</strong></p>",
        unsafe_allow_html=True,
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
