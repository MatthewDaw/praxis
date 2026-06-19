"""
===============================================================================
FILE: app.py
AUTHOR: Monica Peters
CREATED: 2026-06-17
UPDATED: 2026-06-18

PURPOSE:
Streamlit entry point — wires DataProvider to UI components.

USAGE:
    streamlit run app.py

OPERATIONAL:
- PRAXIS_API_BASE_URL unset → mock fixtures (no Matthew backend required)
- All UI logic lives in components/; all data access in services/
===============================================================================
"""

from __future__ import annotations

import os

import streamlit as st

from components.candidate_detail import render_candidate_detail
from components.candidate_list import (
    filter_candidates,
    render_card_view,
    render_last_action_banner,
    render_selection_control,
    render_table_view,
    sync_selected_candidate,
)
from components.eval_metrics_embed import render_eval_metrics_embed
from models.candidate import Candidate
from services.data_provider import DataProvider, get_data_provider

st.set_page_config(
    page_title="PRAXIS Candidate Review Gate",
    page_icon="🧠",
    layout="wide",
)


def _ensure_provider() -> DataProvider:
    if "data_provider" not in st.session_state:
        st.session_state.data_provider = get_data_provider()
    return st.session_state.data_provider


def _render_sidebar_controls() -> None:
    with st.sidebar:
        st.markdown("### Controls")
        if st.button(
            "Refresh data",
            use_container_width=True,
            help="Reload candidates from the API or mock provider after mutations.",
        ):
            st.session_state.pop("data_provider", None)
            st.rerun()
        st.caption(
            "Integration: [candidate-api-v1.md](../docs/integration/candidate-api-v1.md)"
        )


def _render_mode_banner() -> None:
    api_url = os.environ.get("PRAXIS_API_BASE_URL", "").strip()
    if api_url:
        st.caption(f"Live API mode — `{api_url}`")
    else:
        st.caption(
            "Mock mode — local fixtures only. "
            "Matthew's pipeline and Dominic's eval are not required to run this UI."
        )


def _load_candidates(provider: DataProvider) -> tuple[list[Candidate] | None, str | None]:
    """Return candidates or (None, error_message) when the provider fails."""
    try:
        return provider.list_candidates(), None
    except Exception as exc:  # noqa: BLE001 — surface API errors in UI
        return None, str(exc)


provider = _ensure_provider()
_render_sidebar_controls()

st.title("Candidate Review Gate")
st.markdown("Review and promote AI-learned knowledge candidates from agent sessions.")
_render_mode_banner()
render_last_action_banner()

with st.container():
    col1, col2 = st.columns([3, 1])
    with col1:
        search_query = st.text_input(
            "Search",
            placeholder="Search by title or content...",
            help="Filters the candidate list in real time.",
        )
    with col2:
        state_filter = st.selectbox(
            "Filter by State",
            ["All", "proposed", "suggested", "active", "decayed"],
            help="Show only candidates in the selected lifecycle state.",
        )

all_candidates, load_error = _load_candidates(provider)

if load_error:
    st.error(
        f"Backend unavailable — could not load candidates. ({load_error}) "
        "Unset PRAXIS_API_BASE_URL to use mock fixtures locally."
    )
    all_candidates = []

filtered = filter_candidates(all_candidates or [], search_query=search_query, state_filter=state_filter)
selected_for_detail = render_selection_control(filtered)

tab_table, tab_cards = st.tabs(["Table View", "Card View"])

with tab_table:
    render_table_view(
        filtered,
        provider,
        selected_id=selected_for_detail,
        on_action="table",
    )

with tab_cards:
    render_card_view(filtered, provider, on_action="cards")

render_candidate_detail(
    filtered,
    selected_id=sync_selected_candidate(filtered),
    provider=provider,
)
render_eval_metrics_embed()

st.divider()
st.caption(
    "Human-gate dashboard (Monica's pillar) · "
    "Integrates with Matthew's API via PRAXIS_API_BASE_URL · "
    "Does not import pipeline/ or eval/"
)
