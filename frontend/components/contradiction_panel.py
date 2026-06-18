"""Contradiction resolution — side-by-side comparison (Day 5)."""

from __future__ import annotations

import streamlit as st

from models.candidate import Candidate


def render_contradiction_panel(
    candidate: Candidate,
    peers_by_id: dict[str, Candidate],
) -> None:
    """
    Side-by-side contradiction cards with resolution actions.

    Resolution mutations call Matthew's API (Days 6–7); UI-only stub until then.
    """
    st.markdown("**Contradictions**")
    rivals = [peers_by_id[cid] for cid in candidate.contradiction_ids if cid in peers_by_id]

    if not rivals:
        st.info("Contradiction IDs referenced but rival candidates not loaded.")
        return

    for rival in rivals:
        left, right = st.columns(2)
        with left:
            with st.container(border=True):
                st.markdown("**This candidate**")
                st.write(candidate.content)
        with right:
            with st.container(border=True):
                st.markdown(f"**Rival:** {rival.title}")
                st.write(rival.content)

        st.caption(
            "Resolution actions (keep A / keep B / merge) wire to "
            "POST /contradictions/{id}/resolve — Matthew API, Days 6–7."
        )
