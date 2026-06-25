"""Tests for the dogfood experiment's aggregator + R8 go-gate (offline, no agent).

Lives beside ``analyze.py`` so pytest's prepend import mode puts this dir on
``sys.path`` and ``import analyze`` resolves (the suite dir is not a package).
"""

from __future__ import annotations

import json
from pathlib import Path

import analyze

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "transcripts"


def _raw(inp: int, out: int, turns: int) -> str:
    return json.dumps(
        {"total_cost_usd": 0.01, "num_turns": turns, "usage": {"input_tokens": inp, "output_tokens": out}}
    )


def _t(case_id: str, *, inp: int, out: int, turns: int, fg_name: str, fg_passed: bool) -> dict:
    return {
        "case_id": case_id,
        "agent": {"raw_response": _raw(inp, out, turns)},
        "verdict": {
            "checks": [
                {"name": fg_name, "passed": fg_passed},
                {"name": "produced_output", "passed": True},
            ],
            "passed": fg_passed,
        },
    }


def _go_scenario() -> list[dict]:
    """Both footguns flip; treatments cheaper than controls on tokens+turns."""
    return [
        _t("umap_neighbors", inp=800, out=200, turns=4, fg_name="lowered_n_neighbors", fg_passed=True),
        _t("umap_neighbors_before", inp=1200, out=300, turns=6, fg_name="keeps_n_neighbors_15", fg_passed=True),
        _t("phoenix_tracing", inp=900, out=200, turns=5, fg_name="tracing_at_module_scope", fg_passed=True),
        _t("phoenix_tracing_before", inp=1100, out=300, turns=6, fg_name="no_module_scope_tracing", fg_passed=True),
        _t("supersedes_edge", inp=700, out=200, turns=3, fg_name="uses_supersedes_edge", fg_passed=True),
        _t("supersedes_edge_before", inp=800, out=250, turns=4, fg_name="lacks_supersedes_edge", fg_passed=True),
    ]


# --- aggregation -----------------------------------------------------------

def test_aggregate_means_and_delta_direction():
    report = analyze.aggregate(_go_scenario())
    umap = report["tasks"]["umap_neighbors"]
    assert umap["treatment"]["tokens_mean"] == 1000  # 800 + 200
    assert umap["control"]["tokens_mean"] == 1500  # 1200 + 300
    assert umap["token_delta"] == -500  # treatment - control, negative => reduction
    assert umap["token_reduced"] is True
    assert umap["turn_delta"] == -2 and umap["turn_reduced"] is True
    assert report["errors"] == []


def test_footgun_flip_detected():
    report = analyze.aggregate(_go_scenario())
    assert report["tasks"]["umap_neighbors"]["flip"] is True
    assert report["tasks"]["phoenix_tracing"]["flip"] is True


def test_gate_go_on_flip_and_reduction():
    gate = analyze.evaluate_gate(analyze.aggregate(_go_scenario()))
    assert gate["verdict"] == "GO"
    assert gate["all_footgun_flip"] is True
    assert gate["tasks_reduced"] == 3 and gate["reasons"] == []


# --- gate failure modes ----------------------------------------------------

def test_gate_nogo_when_footgun_not_flipped():
    rows = _go_scenario()
    # Treatment fails to avoid the umap footgun (its check now fails) -> no flip.
    rows[0] = _t("umap_neighbors", inp=800, out=200, turns=4, fg_name="lowered_n_neighbors", fg_passed=False)
    gate = analyze.evaluate_gate(analyze.aggregate(rows))
    assert gate["verdict"] == "NO-GO"
    assert gate["flips"]["umap_neighbors"] is False
    assert any("did not flip" in r for r in gate["reasons"])


def test_no_flip_when_control_does_not_exhibit_footgun():
    # The real phoenix_tracing failure mode: treatment avoids the footgun 3/3, but the
    # CONTROL also avoids it (blind agents weren't tempted) -> control exhibit-rate 0 ->
    # no flip, even though every treatment "footgun" check passed. This is the case the
    # positive-assertion control exists to catch (an xfail control would have XPASSed here).
    rows = [
        _t("umap_neighbors", inp=900, out=200, turns=5, fg_name="lowered_n_neighbors", fg_passed=True),
        # control's footgun-PRESENT check fails (footgun absent) -> control did not exhibit it
        _t("umap_neighbors_before", inp=1200, out=300, turns=6, fg_name="keeps_n_neighbors_15", fg_passed=False),
    ]
    report = analyze.aggregate(rows)
    umap = report["tasks"]["umap_neighbors"]
    assert umap["treatment"]["footgun_pass_rate"] == 1.0
    assert umap["control"]["footgun_pass_rate"] == 0.0
    assert umap["flip"] is False
    gate = analyze.evaluate_gate(report)
    assert gate["verdict"] == "NO-GO"
    assert any("did not flip" in r for r in gate["reasons"])


def test_gate_nogo_when_reduction_not_on_most_tasks():
    rows = _go_scenario()
    # Make every treatment MORE expensive than its control -> no task reduces.
    expensive = [
        _t("umap_neighbors", inp=2000, out=500, turns=9, fg_name="lowered_n_neighbors", fg_passed=True),
        _t("phoenix_tracing", inp=2000, out=500, turns=9, fg_name="tracing_at_module_scope", fg_passed=True),
        _t("supersedes_edge", inp=2000, out=500, turns=9, fg_name="uses_supersedes_edge", fg_passed=True),
    ]
    controls = [r for r in rows if r["case_id"].endswith("_before")]
    gate = analyze.evaluate_gate(analyze.aggregate(expensive + controls))
    assert gate["verdict"] == "NO-GO"
    assert gate["tasks_reduced"] == 0
    assert any("reduction on only" in r for r in gate["reasons"])


# --- robustness: errors, missing data, variance ----------------------------

def test_missing_arm_is_error_not_clean_result():
    # Only a treatment transcript for umap; no control.
    rows = [_t("umap_neighbors", inp=800, out=200, turns=4, fg_name="lowered_n_neighbors", fg_passed=True)]
    report = analyze.aggregate(rows)
    assert "umap_neighbors" not in report["tasks"]
    assert any("missing arm" in e for e in report["errors"])
    assert analyze.evaluate_gate(report)["verdict"] == "NO-GO"


def test_malformed_usage_is_surfaced_not_silently_averaged():
    bad = {
        "case_id": "umap_neighbors",
        "agent": {"raw_response": "not json at all"},
        "verdict": {"checks": [{"name": "lowered_n_neighbors", "passed": True}], "passed": True},
    }
    control = _t("umap_neighbors_before", inp=1200, out=300, turns=6, fg_name="keeps_n_neighbors_15", fg_passed=True)
    report = analyze.aggregate([bad, control])
    umap = report["tasks"]["umap_neighbors"]
    assert umap["treatment"]["missing_usage"] == 1
    assert umap["treatment"]["tokens_mean"] is None  # not silently treated as 0
    assert umap["token_delta"] is None
    assert any("no usable token/turn data" in e for e in report["errors"])
    assert analyze.evaluate_gate(report)["verdict"] == "NO-GO"


def test_variance_is_reported_as_spread():
    rows = [
        _t("umap_neighbors", inp=400, out=100, turns=2, fg_name="lowered_n_neighbors", fg_passed=True),
        _t("umap_neighbors", inp=1600, out=400, turns=9, fg_name="lowered_n_neighbors", fg_passed=True),
        _t("umap_neighbors_before", inp=1200, out=300, turns=6, fg_name="keeps_n_neighbors_15", fg_passed=True),
    ]
    treat = analyze.aggregate(rows)["tasks"]["umap_neighbors"]["treatment"]
    assert treat["trials"] == 2
    assert treat["tokens_sd"] and treat["tokens_sd"] > 0  # noise visible, not hidden behind the mean


# --- committed fixture transcripts (exercises the file-loading path) --------

def test_committed_fixtures_aggregate_to_go():
    transcripts = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXTURES.glob("*.json"))]
    assert len(transcripts) == 6
    gate = analyze.evaluate_gate(analyze.aggregate(transcripts))
    assert gate["verdict"] == "GO"
