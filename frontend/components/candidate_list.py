"""Candidate list views — table and card layouts."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from components.confidence_badge import render_confidence_progress, render_state_badge
from models.candidate import Candidate
from services.data_provider import DataProvider


def filter_candidates(
    candidates: list[Candidate],
    *,
    search_query: str,
    state_filter: str,
) -> list[Candidate]:
    """Apply search and lifecycle filters."""
    filtered = candidates
    if search_query:
        q = search_query.casefold()
        filtered = [
            c for c in filtered
            if q in c.title.casefold() or q in c.content.casefold()
        ]
    if state_filter != "All":
        filtered = [c for c in filtered if c.state.value == state_filter]
    return filtered


def _candidates_to_display_frame(candidates: list[Candidate]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "title": c.title,
                "state": c.state.value,
                "confidence": c.confidence,
                "provenance": c.provenance,
                "createdAt": c.created_at,
                "id": c.id,
            }
            for c in candidates
        ]
    )


def render_table_view(
    candidates: list[Candidate],
    provider: DataProvider,
    *,
    on_action: str,
) -> None:
    """Sortable table with promote/reject action row."""
    st.markdown(f"**{len(candidates)} candidates**")

    if not candidates:
        st.info("No candidates match the current filter.")
        return

    display_df = _candidates_to_display_frame(candidates)[
        ["title", "state", "confidence", "provenance", "createdAt"]
    ]

    st.dataframe(
        display_df,
        column_config={
            "title": st.column_config.TextColumn("Title", width="large"),
            "state": st.column_config.TextColumn("State", width="medium"),
            "confidence": st.column_config.ProgressColumn(
                "Confidence",
                help="AI Confidence Score",
                min_value=0,
                max_value=1,
                format="%.2f",
                width="medium",
            ),
            "provenance": st.column_config.TextColumn("Provenance", width="large"),
            "createdAt": st.column_config.DatetimeColumn(
                "Created At", format="MMM DD, YYYY", width="medium"
            ),
        },
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("### Actions")
    action_col1, action_col2 = st.columns([3, 1])
    id_to_title = {c.id: c.title for c in candidates}

    with action_col1:
        selected_id = st.selectbox(
            "Select a candidate to action:",
            list(id_to_title.keys()),
            format_func=lambda cid: id_to_title[cid],
            key=f"table_select_{on_action}",
        )
    with action_col2:
        st.write("")
        st.write("")
        b1, b2 = st.columns(2)
        with b1:
            if st.button("Promote", type="primary", use_container_width=True, key=f"table_promote_{on_action}"):
                _handle_promote(provider, selected_id)
        with b2:
            if st.button("Reject", use_container_width=True, key=f"table_reject_{on_action}"):
                _handle_reject(provider, selected_id)


def render_card_view(
    candidates: list[Candidate],
    provider: DataProvider,
    *,
    on_action: str,
) -> None:
    """Three-column card grid with per-card actions."""
    st.markdown(f"**{len(candidates)} candidates**")

    if not candidates:
        st.info("No candidates match the current filter.")
        return

    cols = st.columns(3)
    for index, candidate in enumerate(candidates):
        col = cols[index % 3]
        with col:
            with st.container(border=True):
                st.subheader(candidate.title)
                render_state_badge(candidate.state.value)
                render_confidence_progress(candidate.confidence)
                st.caption(f"**Source:** `{candidate.provenance}`")
                st.write(candidate.content)
                created = datetime.fromisoformat(candidate.created_at.replace("Z", "+00:00"))
                st.caption(f"Created: {created.strftime('%b %d, %Y')}")

                b1, b2 = st.columns(2)
                with b1:
                    if st.button(
                        "Promote",
                        key=f"card_promo_{candidate.id}_{on_action}",
                        type="primary",
                        use_container_width=True,
                    ):
                        _handle_promote(provider, candidate.id)
                with b2:
                    if st.button(
                        "Reject",
                        key=f"card_rej_{candidate.id}_{on_action}",
                        use_container_width=True,
                    ):
                        _handle_reject(provider, candidate.id)


def _handle_promote(provider: DataProvider, candidate_id: str) -> None:
    from models.candidate import next_promotion_state

    existing = provider.get_candidate(candidate_id)
    if existing is None:
        st.toast("Candidate not found", icon="⚠️")
        st.rerun()
        return
    next_state = next_promotion_state(existing.state)
    if next_state is None:
        st.toast("Candidate is already Active", icon="⚠️")
        st.rerun()
        return
    provider.promote(candidate_id)
    st.toast(f"Promoted candidate to {next_state.value.capitalize()}")
    st.rerun()


def _handle_reject(provider: DataProvider, candidate_id: str) -> None:
    provider.reject(candidate_id)
    st.toast("Rejected candidate")
    st.rerun()
