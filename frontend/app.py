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
from components.candidate_list import filter_candidates, render_card_view, render_table_view
from components.eval_metrics_embed import render_eval_metrics_embed
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


def _render_mode_banner() -> None:
    if os.environ.get("PRAXIS_API_BASE_URL", "").strip():
        st.caption("Live API mode — connected via PRAXIS_API_BASE_URL.")
    else:
        st.caption(
            "Mock mode — local fixtures only. "
            "Matthew's pipeline and Dominic's eval are not required to run this UI."
        )


provider = _ensure_provider()

st.title("Candidate Review Gate")
st.markdown("Review and promote AI-learned knowledge candidates from agent sessions.")
_render_mode_banner()

with st.container():
    col1, col2 = st.columns([3, 1])
    with col1:
        search_query = st.text_input("Search", placeholder="Search by title or content...")
    with col2:
        state_filter = st.selectbox("Filter by State", ["All", "proposed", "suggested", "active"])

all_candidates = provider.list_candidates()
filtered = filter_candidates(all_candidates, search_query=search_query, state_filter=state_filter)

selected_for_detail = filtered[0].id if filtered else None

tab_table, tab_cards = st.tabs(["Table View", "Card View"])

with tab_table:
    render_table_view(filtered, provider, on_action="table")

with tab_cards:
    render_card_view(filtered, provider, on_action="cards")

render_candidate_detail(filtered, selected_id=selected_for_detail)
render_eval_metrics_embed()

st.divider()
st.caption(
    "Human-gate dashboard (Monica's pillar) · "
    "Integrates with Matthew's API via PRAXIS_API_BASE_URL · "
    "Does not import pipeline/ or eval/"
)
