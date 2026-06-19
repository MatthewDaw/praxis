"""Contradiction resolution — side-by-side comparison with resolution actions."""

from __future__ import annotations

import html

import streamlit as st

from models.candidate import Candidate
from services.data_provider import DataProvider


def render_contradiction_panel(
    candidate: Candidate,
    peers_by_id: dict[str, Candidate],
    provider: DataProvider,
) -> None:
    """Side-by-side contradiction cards with keep-A / keep-B / defer actions."""
    st.markdown("**Contradictions**")
    rivals = [peers_by_id[cid] for cid in candidate.contradiction_ids if cid in peers_by_id]

    if not rivals:
        st.info("Contradiction IDs referenced but rival candidates not loaded.")
        return

    for rival in rivals:
        left, right = st.columns(2)
        with left:
            st.markdown(
                f'<div class="compare-card-primary"><strong>This candidate</strong>'
                f"<p>{html.escape(candidate.content)}</p>"
                f"<code>{html.escape(candidate.provenance)}</code></div>",
                unsafe_allow_html=True,
            )
        with right:
            st.markdown(
                f'<div class="compare-card-rival"><strong>Rival: {html.escape(rival.title)}</strong>'
                f"<p>{html.escape(rival.content)}</p>"
                f"<code>{html.escape(rival.provenance)}</code></div>",
                unsafe_allow_html=True,
            )

        contradiction_id = f"{candidate.id}__{rival.id}"
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button(
                "Keep this candidate",
                key=f"resolve_keep_self_{contradiction_id}",
                use_container_width=True,
                help="Resolve in favor of the candidate under review",
            ):
                _resolve(provider, contradiction_id, "keep_primary", candidate.id, rival.title)
        with c2:
            if st.button(
                f"Keep {rival.title[:24]}…" if len(rival.title) > 24 else f"Keep {rival.title}",
                key=f"resolve_keep_rival_{contradiction_id}",
                use_container_width=True,
                help="Resolve in favor of the rival lesson",
            ):
                _resolve(provider, contradiction_id, "keep_rival", rival.id, rival.title)
        with c3:
            if st.button(
                "Defer",
                key=f"resolve_defer_{contradiction_id}",
                use_container_width=True,
                help="Leave both candidates in queue for later review",
            ):
                st.toast(f"Deferred contradiction between {candidate.title} and {rival.title}")
        st.divider()


def _resolve(
    provider: DataProvider,
    contradiction_id: str,
    resolution: str,
    keep_id: str,
    rival_title: str,
) -> None:
    try:
        provider.resolve_contradiction(
            contradiction_id,
            resolution=resolution,
            keep_id=keep_id,
        )
    except NotImplementedError:
        st.error("Contradiction resolution requires Matthew's API (Days 6–7).")
        return
    except (KeyError, ValueError) as exc:
        st.toast(f"Resolution failed: {exc}", icon="⚠️")
        st.rerun()
        return

    st.session_state["last_gate_action"] = (
        f"Resolved contradiction — kept **{keep_id}** over **{rival_title}**."
    )
    st.toast("Contradiction resolved")
    st.rerun()
