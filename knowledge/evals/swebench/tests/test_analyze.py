"""Offline tests for the SWE-rebench pilot analysis (ITT primary + R_exist secondary).

Fully offline — no agents, Docker, or network. The committed
``fixtures/records.sample.json`` is hand-authored with a KNOWN answer, so the
assertions below pin hand-computed values. This is also the ``--from-records``-style
path: U8's CLI loads the same dict and calls :func:`aggregate` on it directly.

    uv run pytest knowledge/evals/swebench/tests/test_analyze.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

from knowledge.evals.swebench import analyze

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "records.sample.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _report():
    d = _load()
    return analyze.aggregate(d["records"], d["rexist_map"], d["instances"])


# --- ITT is unconditioned: computed over ALL records regardless of R_exist ---- #

def test_itt_uses_all_records_including_rexist_zero():
    itt = _report()["itt"]
    t, c = itt["treatment"], itt["control"]
    # all 5 treatment + all 5 control records counted (incl. inst-C with r_exist=False)
    assert t["n_records"] == 5 and c["n_records"] == 5
    # treatment costs [1,2,2,4,3] -> mean 2.4 ; control [3,5,4,6,2] -> mean 4.0
    assert round(t["cost_mean"], 6) == 2.4
    assert round(c["cost_mean"], 6) == 4.0
    # treatment resolved 3/5=0.6 ; control resolved 2/5=0.4
    assert round(t["resolve_rate"], 6) == 0.6
    assert round(c["resolve_rate"], 6) == 0.4
    # directional deltas
    assert round(itt["cost_delta"], 6) == -1.6 and itt["cost_reduced"] is True
    assert round(itt["resolve_delta"], 6) == 0.2 and itt["resolve_improved"] is True


def test_cost_per_resolved_matches_hand_computed():
    itt = _report()["itt"]
    # treatment total cost 12 over 3 resolved -> 4.0 ; control total 20 over 2 -> 10.0
    assert round(itt["treatment"]["cost_per_resolved"], 6) == 4.0
    assert round(itt["control"]["cost_per_resolved"], 6) == 10.0
    # avg cost per instance == arm mean
    assert round(itt["treatment"]["avg_cost_per_instance"], 6) == 2.4


# --- secondary restricted to r_exist==1; differs from ITT --------------------- #

def test_secondary_restricted_to_rexist_instances():
    sec = _report()["secondary"]
    # only inst-A + inst-B feed the secondary (inst-C has r_exist=False, dropped)
    assert sec["treatment"]["n_records"] == 4 and sec["control"]["n_records"] == 4
    # treatment [1,2,2,4] -> 2.25 ; control [3,5,4,6] -> 4.5
    assert round(sec["treatment"]["cost_mean"], 6) == 2.25
    assert round(sec["control"]["cost_mean"], 6) == 4.5
    # treatment resolved 3/4=0.75 ; control resolved 1/4=0.25
    assert round(sec["treatment"]["resolve_rate"], 6) == 0.75
    assert round(sec["control"]["resolve_rate"], 6) == 0.25
    assert sec["label"] == "exploratory" and "exploratory" in sec["note"].lower()


def test_secondary_excludes_the_rexist_zero_instance_that_itt_keeps():
    # inst-C (r_exist=False): control RESOLVES and is cheaper there; including it would
    # pull ITT toward control. Secondary drops it -> ITT and secondary really differ.
    report = _report()
    assert report["itt"]["treatment"]["n_records"] == 5
    assert report["secondary"]["treatment"]["n_records"] == 4
    assert report["itt"]["cost_delta"] != report["secondary"]["cost_delta"]


# --- hit-rate is a separate first-class deliverable --------------------------- #

def test_hit_rate_reported_separately():
    hit = _report()["hit_rate"]
    assert hit["n_instances"] == 3 and hit["n_rexist"] == 2
    assert round(hit["rate"], 6) == round(2 / 3, 6)
    assert hit["instances"] == {"inst-A": True, "inst-B": True, "inst-C": False}


# --- CIs widen as trial count drops ------------------------------------------- #

def test_ci_widens_and_flags_when_trials_are_few():
    # inst-C alone has 1 control record -> CI width must dwarf a well-sampled arm.
    one_rec = [
        {"instance_id": "x", "trial": 0, "arm": "treatment", "resolved": True, "agent_cost": 5.0},
        {"instance_id": "x", "trial": 0, "arm": "control", "resolved": True, "agent_cost": 5.0},
    ]
    many = [
        {"instance_id": f"x{i}", "trial": 0, "arm": "treatment", "resolved": True, "agent_cost": c}
        for i, c in enumerate([1.0, 1.1, 0.9, 1.0, 1.05, 0.95, 1.0, 1.0])
    ] + [
        {"instance_id": f"x{i}", "trial": 0, "arm": "control", "resolved": True, "agent_cost": c}
        for i, c in enumerate([1.0, 1.1, 0.9, 1.0, 1.05, 0.95, 1.0, 1.0])
    ]
    few = analyze.aggregate(one_rec, {})["itt"]["treatment"]
    lots = analyze.aggregate(many, {})["itt"]["treatment"]
    # single record -> flagged low-trial; CI present but uninformative (sd=0 here, still flagged)
    assert few["cost_ci_low_trials"] is True
    assert lots["cost_ci_low_trials"] is False
    # a noisier small sample produces a wider CI than the tight large sample
    noisy_few = analyze.aggregate(
        [{"instance_id": "y", "trial": t, "arm": "treatment", "resolved": True, "agent_cost": c}
         for t, c in enumerate([0.5, 9.5])],
        {},
    )["itt"]["treatment"]
    width_few = noisy_few["cost_ci95"][1] - noisy_few["cost_ci95"][0]
    width_lots = lots["cost_ci95"][1] - lots["cost_ci95"][0]
    assert width_few > width_lots


def test_per_instance_trial_variance_surfaced():
    itt = _report()["itt"]
    var = itt["treatment_trial_variance"]
    # inst-A treatment costs [1,2] -> sd>0 over 2 trials
    assert var["inst-A"]["trials"] == 2 and var["inst-A"]["cost_sd"] > 0


# --- ingestion is a separate amortized line; honest about None placeholders --- #

def test_ingestion_is_placeholder_when_meta_carries_none():
    ing = _report()["ingestion"]
    assert ing["cost_is_placeholder"] is True
    assert ing["total_cost"] is None  # not fabricated as 0
    assert ing["facts_ingested"] == 27  # 14 + 8 + 5


def test_ingestion_sums_when_costs_present():
    instances = [
        {"instance_id": "a", "ingestion_cost": 0.10, "facts_ingested": 3},
        {"instance_id": "b", "ingestion_cost": 0.30, "facts_ingested": 5},
    ]
    ing = analyze._ingestion_line(instances)
    assert ing["cost_is_placeholder"] is False
    assert round(ing["total_cost"], 6) == 0.40
    assert round(ing["amortized_per_instance"], 6) == 0.20


# --- gate: feasibility met; never claims significance ------------------------- #

def test_gate_feasibility_met_on_complete_directional_run():
    d = _load()
    report = analyze.aggregate(d["records"], d["rexist_map"], d["instances"])
    gate = analyze.evaluate_gate(report)
    assert gate["verdict"] == "feasibility met"
    assert gate["significance_claimed"] is False
    assert gate["null_itt_allowed"] is True
    assert gate["harness_complete"] is True
    assert gate["nontrivial_hit_rate"] is True
    assert gate["reasons"] == []


def test_null_itt_alone_does_not_fail_the_gate():
    # Construct a FLAT ITT (treatment == control on cost and resolve), but keep the
    # harness complete, the hit-rate non-trivial, AND a directional secondary. A null
    # primary must NOT flip feasibility to fail.
    records = [
        # inst-A r_exist=True: secondary shows treatment cheaper + resolves more
        {"instance_id": "A", "trial": 0, "arm": "treatment", "resolved": True,  "agent_cost": 1.0},
        {"instance_id": "A", "trial": 0, "arm": "control",   "resolved": False, "agent_cost": 3.0},
        # inst-B r_exist=False: treatment WORSE here, dragging the ITT back to flat
        {"instance_id": "B", "trial": 0, "arm": "treatment", "resolved": False, "agent_cost": 3.0},
        {"instance_id": "B", "trial": 0, "arm": "control",   "resolved": True,  "agent_cost": 1.0},
    ]
    rexist = {"A": {"r_exist": True}, "B": {"r_exist": False}}
    report = analyze.aggregate(records, rexist)
    itt = report["itt"]
    # ITT is exactly flat: means 2.0 vs 2.0, resolve 0.5 vs 0.5
    assert round(itt["cost_delta"], 6) == 0.0 and itt["cost_reduced"] is False
    assert round(itt["resolve_delta"], 6) == 0.0 and itt["resolve_improved"] is False
    assert analyze._directional(itt) is False  # primary shows nothing
    # but the secondary IS directional -> gate stays met
    assert analyze._directional(report["secondary"]) is True
    gate = analyze.evaluate_gate(report)
    assert gate["verdict"] == "feasibility met"
    assert gate["itt_directional"] is False
    assert gate["secondary_directional"] is True


def test_gate_fails_on_trivial_hit_rate():
    records = [
        {"instance_id": "A", "trial": 0, "arm": "treatment", "resolved": True,  "agent_cost": 1.0},
        {"instance_id": "A", "trial": 0, "arm": "control",   "resolved": False, "agent_cost": 3.0},
    ]
    rexist = {"A": {"r_exist": False}}  # zero relevant knowledge anywhere
    gate = analyze.evaluate_gate(analyze.aggregate(records, rexist))
    assert gate["verdict"] == "feasibility not met"
    assert gate["nontrivial_hit_rate"] is False
    assert any("hit-rate trivial" in r for r in gate["reasons"])


def test_gate_fails_when_an_arm_is_missing():
    records = [
        {"instance_id": "A", "trial": 0, "arm": "treatment", "resolved": True, "agent_cost": 1.0},
    ]
    report = analyze.aggregate(records, {"A": {"r_exist": True}})
    assert any("no control records" in e for e in report["errors"])
    gate = analyze.evaluate_gate(report)
    assert gate["verdict"] == "feasibility not met"
    assert gate["harness_complete"] is False


# --- --from-records path: re-aggregate the committed sample, no agents/Docker - #

def test_from_records_reaggregates_committed_sample_offline():
    d = _load()  # the whole dict, exactly as U8's --from-records will load it
    report = analyze.aggregate(d["records"], d["rexist_map"], d["instances"])
    gate = analyze.evaluate_gate(report)
    # report dict exposes the documented top-level keys
    assert set(report) >= {"itt", "secondary", "hit_rate", "ingestion", "errors"}
    # format_report renders without error and mentions the framing
    text = analyze.format_report(report, gate)
    assert "ITT" in text and "EXPLORATORY" in text and "hit-rate" in text.lower()
    assert "feasibility met" in text
