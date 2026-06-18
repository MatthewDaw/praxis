"""Optional eval metrics embed for Dominic's compounding curve (Day 8)."""

from __future__ import annotations

import pandas as pd
import streamlit as st


def render_eval_metrics_embed() -> None:
    """
    Placeholder for eval harness metrics — Dominic owns computation in eval/.

    This component only renders data Dominic exposes via API or static JSON;
    it does not import eval/ or compute compounding curves locally.
    """
    with st.expander("Eval metrics (Day 8 — Dominic integration)", expanded=False):
        st.caption(
            "Compounding curve and before/after scoreboard embed here when "
            "eval harness outputs are available via a UI-agnostic metrics endpoint."
        )
        st.line_chart(
            pd.DataFrame({"correction_rate": [1.0, 0.72, 0.48, 0.35]}),
            height=200,
        )
        st.caption("Placeholder curve — replace with Dominic's eval API response.")
