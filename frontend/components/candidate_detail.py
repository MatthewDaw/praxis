"""Candidate detail panel — full content, confidence, provenance, audit trail."""

from __future__ import annotations

import html
from typing import Any

import streamlit as st

from components.confidence_badge import render_confidence_breakdown, render_state_badge
from components.contradiction_panel import render_contradiction_panel
from models.candidate import Candidate
from services.data_provider import DataProvider


def render_candidate_detail(
    candidates: list[Candidate],
    *,
    selected_id: str | None,
    provider: DataProvider,
) -> None:
    """Detail view for a selected candidate — right-column panel."""
    st.markdown('<div class="detail-panel-wrap">', unsafe_allow_html=True)
    st.markdown(
        '<p style="font-size:0.78rem;font-weight:600;text-transform:uppercase;'
        'letter-spacing:0.05em;color:#6b7280;margin:0 0 0.5rem;">Candidate detail</p>',
        unsafe_allow_html=True,
    )

    if not candidates:
        st.caption("Select a candidate from the list to inspect details.")
        st.markdown("</div>", unsafe_allow_html=True)
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
        help="Keyboard: Tab here, then use arrow keys to inspect another candidate.",
    )
    st.session_state["selected_candidate_id"] = detail_id
    candidate = id_to_candidate[detail_id]

    st.markdown(
        f'<h2 style="margin:0 0 1rem;font-size:1.15rem;color:#111827;">'
        f"{html.escape(candidate.title)}</h2>",
        unsafe_allow_html=True,
    )

    meta1, meta2 = st.columns(2)
    with meta1:
        st.markdown("**State**")
        render_state_badge(candidate.display_state, candidate.state)
    with meta2:
        st.markdown("**Created**")
        st.write(candidate.created_at[:10] if candidate.created_at else "—")

    st.markdown("**Provenance**")
    st.code(candidate.provenance, language=None)

    st.markdown("**Content**")
    st.write(candidate.content)

    st.markdown("**Confidence**")
    render_confidence_breakdown(candidate)

    st.markdown("**Audit trail**")
    _render_audit_trail(candidate)

    if candidate.extra:
        pipeline_extra = {
            key: value for key, value in candidate.extra.items() if key != "auditTrail"
        }
        if pipeline_extra:
            with st.expander("Additional pipeline fields"):
                st.json(pipeline_extra)

    if candidate.contradiction_ids:
        render_contradiction_panel(candidate, id_to_candidate, provider)
    else:
        st.markdown(
            '<p class="status-ok"><span aria-hidden="true">✓</span> '
            "No contradictions flagged for this candidate.</p>",
            unsafe_allow_html=True,
        )

    st.markdown("</div>", unsafe_allow_html=True)


def _render_audit_trail(candidate: Candidate) -> None:
    """Show promotion/scoring history with JSONL provenance links."""
    entries = _audit_entries(candidate)
    if not entries:
        st.caption(
            f"Created {candidate.created_at} · Source log line `{candidate.provenance}`. "
            "Full audit events arrive from Matthew's API in live mode."
        )
        return

    items_html = []
    for entry in entries:
        action = html.escape(str(entry.get("action", "event")))
        timestamp = html.escape(str(entry.get("timestamp", candidate.created_at)))
        provenance = html.escape(str(entry.get("provenance", candidate.provenance)))
        actor = html.escape(str(entry.get("actor", "system")))
        note = entry.get("note")
        note_html = f" — {html.escape(str(note))}" if note else ""
        items_html.append(
            f'<li><div class="audit-timeline__action">{action}</div>'
            f'<div style="font-size:0.82rem;color:#6b7280;margin-top:2px;">'
            f"{timestamp} · <code>{provenance}</code> · <em>{actor}</em>{note_html}</div></li>"
        )

    st.markdown(
        f'<ul class="audit-timeline">{"".join(items_html)}</ul>',
        unsafe_allow_html=True,
    )


def _audit_entries(candidate: Candidate) -> list[dict[str, Any]]:
    raw = candidate.extra.get("auditTrail") or candidate.extra.get("audit_trail")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []
