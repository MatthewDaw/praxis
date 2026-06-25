"""Tests for the dogfood v2 cost-to-correct aggregator + go-gate (offline, no agent).

Lives beside ``analyze.py`` so pytest's prepend import mode puts this dir on
``sys.path`` and ``import analyze`` resolves (the suite dir is not a package).
"""

from __future__ import annotations

import json
from pathlib import Path

import analyze

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "trials.sample.json"


def _rec(task, kind, *, tc, tok, cc, cok, rework=None, tt=4, ct=6):
    """A per-trial record. tc/cc = treat/control cost; tok/cok = correct flags."""
    return {
        "task": task, "kind": kind,
        "treat": {"cost": tc, "turns": tt, "tokens": 1000, "correct": tok},
        "control": {"cost": cc, "turns": ct, "tokens": 1500, "correct": cok},
        "rework": ({"cost": rework} if rework is not None else None),
    }


def _go_records():
    """Both footguns flip; cost-to-correct cheaper on all four tasks."""
    return [
        # footgun: treatment avoids, control exhibits + pays rework
        _rec("umap_neighbors", "footgun", tc=0.04, tok=True, cc=0.05, cok=False, rework=0.03),
        _rec("yoyo_lazy_import", "footgun", tc=0.04, tok=True, cc=0.05, cok=False, rework=0.03),
        # convention: not a footgun flip, but control was wrong and paid rework
        _rec("supersedes_edge", "convention", tc=0.05, tok=True, cc=0.03, cok=False, rework=0.04),
        # quantitative: no correctness notion, no rework; treatment just cheaper
        _rec("repo_mounted_dsn", "quantitative", tc=0.03, tok=None, cc=0.05, cok=None),
    ]


# --- aggregation -----------------------------------------------------------

def test_cost_to_correct_charges_rework_to_control():
    report = analyze.aggregate(_go_records())
    umap = report["tasks"]["umap_neighbors"]
    assert umap["treat_cost_mean"] == 0.04
    assert umap["ctc_cost_mean"] == 0.08  # control 0.05 + rework 0.03
    assert round(umap["cost_delta"], 4) == -0.04  # treatment cheaper to-correct
    assert umap["cost_reduced"] is True


def test_quantitative_task_has_no_rework_and_no_flip():
    report = analyze.aggregate(_go_records())
    dsn = report["tasks"]["repo_mounted_dsn"]
    assert dsn["is_footgun"] is False
    assert dsn["ctc_cost_mean"] == 0.05  # control first-pass only, no rework
    assert dsn["cost_reduced"] is True
    assert dsn["flip"] is False


def test_footgun_flip_detected_for_footgun_tasks_only():
    report = analyze.aggregate(_go_records())
    assert report["tasks"]["umap_neighbors"]["flip"] is True
    assert report["tasks"]["yoyo_lazy_import"]["flip"] is True
    assert report["tasks"]["supersedes_edge"]["flip"] is False  # convention, not footgun


def test_gate_go_on_flips_and_cost_reduction():
    gate = analyze.evaluate_gate(analyze.aggregate(_go_records()))
    assert gate["verdict"] == "GO"
    assert gate["all_footgun_flip"] is True
    assert gate["tasks_reduced"] == 4 and gate["reasons"] == []


# --- gate failure modes ----------------------------------------------------

def test_nogo_when_treatment_fails_to_avoid_footgun():
    recs = _go_records()
    recs[0] = _rec("umap_neighbors", "footgun", tc=0.04, tok=False, cc=0.05, cok=False, rework=0.03)
    gate = analyze.evaluate_gate(analyze.aggregate(recs))
    assert gate["verdict"] == "NO-GO"
    assert gate["flips"]["umap_neighbors"] is False
    assert any("did not flip" in r for r in gate["reasons"])


def test_nogo_when_control_does_not_exhibit_footgun():
    # The real phoenix failure mode: treatment avoids AND control also avoids
    # (blind agents weren't tempted) -> control exhibit-rate 0 -> no flip. The
    # control got it right blind, so there is no rework to charge.
    recs = _go_records()
    recs[1] = _rec("yoyo_lazy_import", "footgun", tc=0.04, tok=True, cc=0.03, cok=True)
    report = analyze.aggregate(recs)
    yoyo = report["tasks"]["yoyo_lazy_import"]
    assert yoyo["control_exhibit_rate"] == 0.0
    assert yoyo["flip"] is False
    gate = analyze.evaluate_gate(report)
    assert gate["verdict"] == "NO-GO"
    assert any("did not flip" in r for r in gate["reasons"])


def test_nogo_when_cost_to_correct_not_cheaper_on_most_tasks():
    # Treatments expensive everywhere -> no task's cost-to-correct drops.
    recs = [
        _rec("umap_neighbors", "footgun", tc=0.20, tok=True, cc=0.05, cok=False, rework=0.03),
        _rec("yoyo_lazy_import", "footgun", tc=0.20, tok=True, cc=0.05, cok=False, rework=0.03),
        _rec("supersedes_edge", "convention", tc=0.20, tok=True, cc=0.03, cok=False, rework=0.04),
        _rec("repo_mounted_dsn", "quantitative", tc=0.20, tok=None, cc=0.05, cok=None),
    ]
    gate = analyze.evaluate_gate(analyze.aggregate(recs))
    assert gate["verdict"] == "NO-GO"
    assert gate["tasks_reduced"] == 0
    assert any("cost-to-correct dropped on only" in r for r in gate["reasons"])


def test_missing_arm_cost_is_an_error_not_a_clean_result():
    rec = _rec("umap_neighbors", "footgun", tc=None, tok=True, cc=0.05, cok=False, rework=0.03)
    report = analyze.aggregate([rec])
    assert "umap_neighbors" not in report["tasks"]
    assert any("missing cost data" in e for e in report["errors"])
    assert analyze.evaluate_gate(report)["verdict"] == "NO-GO"


def test_variance_reported_as_spread():
    recs = [
        _rec("umap_neighbors", "footgun", tc=0.02, tok=True, cc=0.05, cok=False, rework=0.03),
        _rec("umap_neighbors", "footgun", tc=0.10, tok=True, cc=0.05, cok=False, rework=0.03),
    ]
    umap = analyze.aggregate(recs)["tasks"]["umap_neighbors"]
    assert umap["trials"] == 2
    assert umap["treat_cost_sd"] and umap["treat_cost_sd"] > 0


# --- rework-trigger predicate (orchestration-side logic) -------------------

def test_should_rework_only_when_task_has_check_and_control_wrong():
    check = ("ref", {})
    assert analyze._should_rework(check, False) is True       # wrong control on a checked task
    assert analyze._should_rework(check, True) is False        # control was correct -> no rework
    assert analyze._should_rework(None, False) is False        # quantitative task -> never reworks
    assert analyze._should_rework(check, None) is False        # indeterminate control -> no rework


# --- committed fixture (file-loading path) ---------------------------------

def test_committed_fixture_aggregates_to_go():
    records = json.loads(FIXTURE.read_text(encoding="utf-8"))["records"]
    gate = analyze.evaluate_gate(analyze.aggregate(records))
    assert gate["verdict"] == "GO"
