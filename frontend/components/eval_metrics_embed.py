"""Optional eval metrics embed for Dominic's compounding curve (Day 8)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pandas as pd
import streamlit as st

_DEFAULT_CURVE = pd.DataFrame({"correction_rate": [1.0, 0.72, 0.48, 0.35]})


def render_eval_metrics_embed() -> None:
    """
    Eval harness metrics — Dominic owns computation in eval/.

    Set PRAXIS_EVAL_METRICS_URL to a JSON endpoint returning:
    {"correction_rate": [1.0, 0.8, ...], "sessions": [...]} (sessions optional)
    """
    with st.expander("Eval metrics — compounding curve", expanded=False):
        metrics = _load_eval_metrics()
        if metrics.get("source") == "placeholder":
            st.caption(
                "Placeholder curve — set PRAXIS_EVAL_METRICS_URL to Dominic's eval metrics "
                "endpoint when available."
            )
        else:
            st.caption(f"Loaded from {metrics['source']}")

        series = metrics.get("correction_rate") or metrics.get("correctionRate")
        if isinstance(series, list) and series:
            frame = pd.DataFrame({"correction_rate": series})
            if isinstance(metrics.get("sessions"), list) and len(metrics["sessions"]) == len(series):
                frame.index = metrics["sessions"]
            st.line_chart(frame, height=220)
        else:
            st.line_chart(_DEFAULT_CURVE, height=220)

        before = metrics.get("corrections_before") or metrics.get("correctionsBefore")
        after = metrics.get("corrections_after") or metrics.get("correctionsAfter")
        if before is not None and after is not None:
            c1, c2, c3 = st.columns(3)
            with c1:
                st.metric("Corrections (cold)", before)
            with c2:
                st.metric("Corrections (with PRAXIS)", after)
            with c3:
                try:
                    reduction = (1 - float(after) / float(before)) * 100
                    st.metric("Reduction", f"{reduction:.0f}%")
                except (TypeError, ZeroDivisionError):
                    st.metric("Reduction", "—")


def _load_eval_metrics() -> dict:
    url = os.environ.get("PRAXIS_EVAL_METRICS_URL", "").strip()
    if not url:
        return {"source": "placeholder", "correction_rate": _DEFAULT_CURVE["correction_rate"].tolist()}

    try:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "X-Praxis-Contract": "1"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if isinstance(payload, dict):
            payload["source"] = url
            return payload
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as exc:
        st.warning(f"Eval metrics unavailable ({exc}) — showing placeholder curve.")
    return {"source": "placeholder", "correction_rate": _DEFAULT_CURVE["correction_rate"].tolist()}
