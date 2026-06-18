"""Candidate detail panel — full content, confidence, provenance (Day 3)."""

from __future__ import annotations

import streamlit as st

from components.confidence_badge import render_confidence_breakdown
from components.contradiction_panel import render_contradiction_panel
from models.candidate import Candidate


def render_candidate_detail(
    candidates: list[Candidate],
    *,
    selected_id: str | None,
) -> None:
    """Expandable detail view for a selected candidate."""
    with st.expander("Candidate detail (Day 3)", expanded=selected_id is not None):
        if not candidates:
            st.caption("Select a candidate from the list views above.")
            return

        id_to_candidate = {c.id: c for c in candidates}
        options = list(id_to_candidate.keys())
        default_index = options.index(selected_id) if selected_id in id_to_candidate else 0

        detail_id = st.selectbox(
            "Inspect candidate",
            options,
            index=default_index,
            format_func=lambda cid: id_to_candidate[cid].title,
            key="detail_candidate_select",
        )
        candidate = id_to_candidate[detail_id]

        st.subheader(candidate.title)
        st.markdown(f"**State:** `{candidate.state.value}`")
        st.markdown(f"**Provenance:** `{candidate.provenance}`")
        st.markdown("**Content**")
        st.write(candidate.content)

        st.markdown("**Confidence**")
        render_confidence_breakdown(candidate)

        st.markdown("**Audit trail**")
        st.caption(
            f"Created {candidate.created_at} · Source log line linked above. "
            "Full JSONL audit wiring lands Days 6–7 with Matthew's API."
        )

        if candidate.contradiction_ids:
            render_contradiction_panel(candidate, id_to_candidate)
        else:
            st.caption("No contradictions flagged for this candidate.")
