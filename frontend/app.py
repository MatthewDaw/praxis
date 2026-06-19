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
from pathlib import Path

import streamlit as st

from components.candidate_detail import render_candidate_detail
from components.candidate_list import (
    filter_candidates,
    render_card_view,
    render_count_chip,
    render_last_action_banner,
    render_table_view,
    sync_selected_candidate,
)
from components.eval_metrics_embed import render_eval_metrics_embed
from models.candidate import Candidate
from services.data_provider import DataProvider, get_data_provider

st.set_page_config(
    page_title="PRAXIS Knowledge Graph Dashboard",
    page_icon="🧠",
    layout="wide",
)

_CSS_PATH = Path(__file__).resolve().parent / "static" / "dashboard.css"


def _inject_dashboard_css() -> None:
    if _CSS_PATH.is_file():
        st.markdown(f"<style>{_CSS_PATH.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


def _ensure_provider() -> DataProvider:
    if "data_provider" not in st.session_state:
        st.session_state.data_provider = get_data_provider()
    return st.session_state.data_provider


def _render_header() -> None:
    api_url = os.environ.get("PRAXIS_API_BASE_URL", "").strip()
    badge_class = "env-badge env-badge--live" if api_url else "env-badge env-badge--mock"
    badge_text = (
        f"Live API · <code>{api_url}</code>" if api_url else "Mock mode — local fixtures only"
    )

    st.markdown(
        f"""
        <div class="praxis-header">
          <div class="praxis-brand">PRAXIS</div>
          <h1>Candidate Review Gate</h1>
          <p>Review and promote AI-learned knowledge candidates from agent sessions.</p>
          <p><span class="{badge_class}">{badge_text}</span></p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    action_col1, action_col2 = st.columns([1, 3])
    with action_col1:
        if st.button(
            "Refresh data",
            type="primary",
            use_container_width=True,
            help="Reload candidates from the API or mock provider after mutations.",
        ):
            st.session_state.pop("data_provider", None)
            st.rerun()
    with action_col2:
        st.caption(
            "Contract: [candidate-api-v1.md](../docs/integration/candidate-api-v1.md) · "
            "Matthew implements the server; this Streamlit client targets the same endpoints "
            "as the React dashboard in `frontend-react/`."
        )


def _load_candidates(provider: DataProvider) -> tuple[list[Candidate] | None, str | None]:
    """Return candidates or (None, error_message) when the provider fails."""
    try:
        return provider.list_candidates(), None
    except Exception as exc:  # noqa: BLE001 — surface API errors in UI
        return None, str(exc)


_inject_dashboard_css()
provider = _ensure_provider()
_render_header()
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
            "Filter by state",
            ["All", "proposed", "suggested", "active", "decayed"],
            help="Show only candidates in the selected lifecycle state.",
        )

with st.spinner("Loading candidates…"):
    all_candidates, load_error = _load_candidates(provider)

if load_error:
    st.error(
        f"Backend unavailable — could not load candidates. ({load_error}) "
        "Unset PRAXIS_API_BASE_URL to use mock fixtures locally."
    )
    all_candidates = []

filtered = filter_candidates(all_candidates or [], search_query=search_query, state_filter=state_filter)
render_count_chip(len(filtered))
selected_for_detail = sync_selected_candidate(filtered)

list_col, detail_col = st.columns([3, 2], gap="large")

with list_col:
    tab_table, tab_cards = st.tabs(["Table view", "Card view"])

    with tab_table:
        render_table_view(
            filtered,
            provider,
            selected_id=selected_for_detail,
            on_action="table",
        )

    with tab_cards:
        render_card_view(filtered, provider, on_action="cards")

with detail_col:
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
