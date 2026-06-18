"""Candidate detail panel — full content, confidence, provenance, audit trail."""

from __future__ import annotations

from typing import Any

import streamlit as st

from components.confidence_badge import render_confidence_breakdown
from components.contradiction_panel import render_contradiction_panel
from models.candidate import Candidate
from services.data_provider import DataProvider


def render_candidate_detail(
    candidates: list[Candidate],
    *,
    selected_id: str | None,
    provider: DataProvider,
) -> None:
    """Expandable detail view for a selected candidate."""
    with st.expander("Candidate detail", expanded=selected_id is not None):
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
            help="Keyboard: Tab here, then use arrow keys to inspect another candidate.",
        )
        candidate = id_to_candidate[detail_id]

        st.subheader(candidate.title)
        st.markdown(f"**State:** `{candidate.display_state}`")
        st.markdown(f"**Provenance:** `{candidate.provenance}`")
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
            st.caption("No contradictions flagged for this candidate.")


def _render_audit_trail(candidate: Candidate) -> None:
    """Show promotion/scoring history with JSONL provenance links."""
    entries = _audit_entries(candidate)
    if not entries:
        st.caption(
            f"Created {candidate.created_at} · Source log line `{candidate.provenance}`. "
            "Full audit events arrive from Matthew's API in live mode."
        )
        return

    for entry in entries:
        action = entry.get("action", "event")
        timestamp = entry.get("timestamp", candidate.created_at)
        provenance = entry.get("provenance", candidate.provenance)
        actor = entry.get("actor", "system")
        note = entry.get("note")
        line = f"- **{action}** · {timestamp} · `{provenance}` · _{actor}_"
        if note:
            line += f" — {note}"
        st.markdown(line)


def _audit_entries(candidate: Candidate) -> list[dict[str, Any]]:
    raw = candidate.extra.get("auditTrail") or candidate.extra.get("audit_trail")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []
