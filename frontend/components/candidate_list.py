"""Candidate list views — table and card layouts."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from components.confidence_badge import render_confidence_progress, render_state_badge
from models.candidate import Candidate, CandidateState, next_promotion_state
from services.data_provider import DataProvider

_SELECTED_KEY = "selected_candidate_id"
_CONFIRM_PROMOTE_KEY = "confirm_promote_id"
_CONFIRM_REJECT_KEY = "confirm_reject_id"
_CONFIRM_REJECT_REASON_KEY = "confirm_reject_reason"
_LAST_ACTION_KEY = "last_gate_action"
_LOW_CONFIDENCE_THRESHOLD = 0.5


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
        filtered = [c for c in filtered if c.display_state == state_filter]
    return filtered


def sync_selected_candidate(candidates: list[Candidate]) -> str | None:
    """Keep session selection valid against the current filtered list."""
    if not candidates:
        st.session_state.pop(_SELECTED_KEY, None)
        return None

    valid_ids = {c.id for c in candidates}
    current = st.session_state.get(_SELECTED_KEY)
    if current not in valid_ids:
        st.session_state[_SELECTED_KEY] = candidates[0].id
    return st.session_state[_SELECTED_KEY]


def render_count_chip(count: int) -> None:
    """Candidate count chip aligned with React FilterBar."""
    st.markdown(
        f'<p style="margin:0.5rem 0 1rem;"><span class="count-chip">{count} candidates</span></p>',
        unsafe_allow_html=True,
    )


def render_selection_control(candidates: list[Candidate]) -> str | None:
    """Shared selectbox driving detail view and table actions."""
    if not candidates:
        return None

    id_to_candidate = {c.id: c for c in candidates}
    options = list(id_to_candidate.keys())
    selected_id = sync_selected_candidate(candidates)
    default_index = options.index(selected_id) if selected_id in options else 0

    chosen = st.selectbox(
        "Selected candidate (detail + table actions)",
        options,
        index=default_index,
        format_func=lambda cid: f"{id_to_candidate[cid].title} ({id_to_candidate[cid].display_state})",
        key="global_candidate_select",
        help="Keyboard: Tab to this control, then arrow keys to change selection.",
    )
    st.session_state[_SELECTED_KEY] = chosen
    return chosen


def render_last_action_banner() -> None:
    """Show feedback from the most recent promote/reject action."""
    action = st.session_state.pop(_LAST_ACTION_KEY, None)
    if action:
        st.success(action)


def render_table_view(
    candidates: list[Candidate],
    provider: DataProvider,
    *,
    selected_id: str | None,
    on_action: str,
) -> None:
    """Sortable table with promote/reject action row."""
    if not candidates:
        st.info("No candidates match the current filter. Try clearing search or choosing **All** states.")
        return

    st.markdown('<div class="list-panel-wrap">', unsafe_allow_html=True)

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

    st.markdown("</div>", unsafe_allow_html=True)

    if selected_id is None:
        return

    st.markdown('<div class="actions-block">', unsafe_allow_html=True)
    st.markdown("### Actions")
    action_col1, action_col2 = st.columns([3, 1])
    id_to_title = {c.id: c.title for c in candidates}

    with action_col1:
        st.caption(f"Acting on: **{id_to_title.get(selected_id, selected_id)}**")
    with action_col2:
        st.write("")
        st.write("")
        b1, b2 = st.columns(2)
        with b1:
            if st.button(
                "Promote",
                type="primary",
                use_container_width=True,
                key=f"table_promote_{on_action}",
                help="Advance proposed → suggested → active",
            ):
                _begin_promote(provider, selected_id)
        with b2:
            if st.button(
                "Reject",
                use_container_width=True,
                key=f"table_reject_{on_action}",
                help="Remove candidate from review queue",
            ):
                _begin_reject(selected_id)

    _render_confirmation_dialogs(provider)
    st.markdown("</div>", unsafe_allow_html=True)


def render_card_view(
    candidates: list[Candidate],
    provider: DataProvider,
    *,
    on_action: str,
) -> None:
    """Three-column card grid with per-card actions."""
    if not candidates:
        st.info("No candidates match the current filter. Try clearing search or choosing **All** states.")
        return

    cols = st.columns(3)
    for index, candidate in enumerate(candidates):
        col = cols[index % 3]
        with col:
            with st.container(border=True):
                st.subheader(candidate.title)
                render_state_badge(candidate.display_state, candidate.state)
                render_confidence_progress(candidate.confidence)
                st.caption(f"**Source:** `{candidate.provenance}`")
                st.write(candidate.content)
                created = datetime.fromisoformat(candidate.created_at.replace("Z", "+00:00"))
                st.caption(f"Created: {created.strftime('%b %d, %Y')}")

                if st.button(
                    "Inspect in detail",
                    key=f"card_inspect_{candidate.id}_{on_action}",
                    use_container_width=True,
                    help="Select this candidate in the detail panel below",
                ):
                    st.session_state[_SELECTED_KEY] = candidate.id
                    st.rerun()

                b1, b2 = st.columns(2)
                with b1:
                    if st.button(
                        "Promote",
                        key=f"card_promo_{candidate.id}_{on_action}",
                        type="primary",
                        use_container_width=True,
                        help="Advance lifecycle state",
                    ):
                        _begin_promote(provider, candidate.id)
                with b2:
                    if st.button(
                        "Reject",
                        key=f"card_rej_{candidate.id}_{on_action}",
                        use_container_width=True,
                        help="Remove from review queue",
                    ):
                        _begin_reject(candidate.id)

    _render_confirmation_dialogs(provider)


def _candidates_to_display_frame(candidates: list[Candidate]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "title": c.title,
                "state": c.display_state,
                "confidence": c.confidence,
                "provenance": c.provenance,
                "createdAt": c.created_at,
                "id": c.id,
            }
            for c in candidates
        ]
    )


def _begin_promote(provider: DataProvider, candidate_id: str) -> None:
    existing = provider.get_candidate(candidate_id)
    if existing is None:
        st.session_state[_LAST_ACTION_KEY] = "Candidate not found — list may be stale."
        st.session_state.pop(_CONFIRM_PROMOTE_KEY, None)
        st.rerun()
        return
    if next_promotion_state(existing.state) is None:
        st.session_state[_LAST_ACTION_KEY] = (
            f"**{existing.title}** is already **{existing.display_state}** — no further promotion."
        )
        st.rerun()
        return
    if existing.state is CandidateState.DECAYED:
        st.session_state[_LAST_ACTION_KEY] = (
            f"**{existing.title}** is **decayed** — restore via pipeline before promoting."
        )
        st.rerun()
        return
    st.session_state[_CONFIRM_PROMOTE_KEY] = candidate_id
    st.session_state.pop(_CONFIRM_REJECT_KEY, None)
    st.rerun()


def _begin_reject(candidate_id: str) -> None:
    st.session_state[_CONFIRM_REJECT_KEY] = candidate_id
    st.session_state.pop(_CONFIRM_PROMOTE_KEY, None)
    st.rerun()


def _render_confirmation_dialogs(provider: DataProvider) -> None:
    promote_id = st.session_state.get(_CONFIRM_PROMOTE_KEY)
    if promote_id:
        candidate = provider.get_candidate(promote_id)
        if candidate is None:
            st.session_state.pop(_CONFIRM_PROMOTE_KEY, None)
            return
        next_state = next_promotion_state(candidate.state)
        if next_state is None:
            st.session_state.pop(_CONFIRM_PROMOTE_KEY, None)
            return
        st.warning(
            f"Promote **{candidate.title}** from **{candidate.display_state}** "
            f"to **{next_state.value}**?"
        )
        if candidate.confidence < _LOW_CONFIDENCE_THRESHOLD:
            st.warning(
                f"Confidence is **{candidate.confidence:.0%}** (below {_LOW_CONFIDENCE_THRESHOLD:.0%}) — "
                "confirm you want to promote a low-confidence lesson."
            )
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Confirm promote", type="primary", key="confirm_promote_yes"):
                _execute_promote(provider, promote_id, candidate.title, next_state)
        with c2:
            if st.button("Cancel", key="confirm_promote_no"):
                st.session_state.pop(_CONFIRM_PROMOTE_KEY, None)
                st.rerun()
        return

    reject_id = st.session_state.get(_CONFIRM_REJECT_KEY)
    if reject_id:
        candidate = provider.get_candidate(reject_id)
        title = candidate.title if candidate else reject_id
        st.warning(f"Reject **{title}** and remove it from the review queue?")
        reason = st.text_input(
            "Rejection reason (optional)",
            key=_CONFIRM_REJECT_REASON_KEY,
            help="Sent to Matthew's API as the reject reason when using live mode.",
        )
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Confirm reject", type="primary", key="confirm_reject_yes"):
                _execute_reject(provider, reject_id, title, reason=reason.strip() or None)
        with c2:
            if st.button("Cancel", key="confirm_reject_no"):
                st.session_state.pop(_CONFIRM_REJECT_KEY, None)
                st.session_state.pop(_CONFIRM_REJECT_REASON_KEY, None)
                st.rerun()


def _execute_promote(
    provider: DataProvider,
    candidate_id: str,
    title: str,
    next_state: CandidateState,
) -> None:
    try:
        provider.promote(candidate_id)
    except KeyError as exc:
        st.session_state[_LAST_ACTION_KEY] = f"Promote failed: {exc}"
    except ValueError as exc:
        st.session_state[_LAST_ACTION_KEY] = f"Promote failed: {exc}"
    except Exception as exc:
        from services.api_client import ApiConflictError

        if isinstance(exc, ApiConflictError):
            st.session_state[_LAST_ACTION_KEY] = (
                f"Promote conflict (409) for **{title}** — refresh and retry. ({exc})"
            )
        else:
            st.session_state[_LAST_ACTION_KEY] = f"Promote failed: {exc}"
    else:
        st.session_state[_LAST_ACTION_KEY] = (
            f"Promoted **{title}** to **{next_state.value}**."
        )
    st.session_state.pop(_CONFIRM_PROMOTE_KEY, None)
    st.rerun()


def _execute_reject(
    provider: DataProvider,
    candidate_id: str,
    title: str,
    *,
    reason: str | None = None,
) -> None:
    try:
        provider.reject(candidate_id, reason=reason)
    except KeyError as exc:
        st.session_state[_LAST_ACTION_KEY] = f"Reject failed: {exc}"
    else:
        note = f" (reason: {reason})" if reason else ""
        st.session_state[_LAST_ACTION_KEY] = f"Rejected **{title}**{note}."
    st.session_state.pop(_CONFIRM_REJECT_KEY, None)
    st.session_state.pop(_CONFIRM_REJECT_REASON_KEY, None)
    st.rerun()
