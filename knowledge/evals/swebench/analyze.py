"""Pure analysis for the SWE-rebench PR-knowledge pilot — ITT primary, R_exist secondary.

WHY this framing (it is a feasibility study, not a verdict):

* **Primary = ITT (intention-to-treat), unconditioned.** We compare ALL treatment
  records against ALL control records regardless of whether relevant knowledge
  existed or whether retrieval actually fired. Conditioning the primary estimate on
  a post-randomization event (did retrieval help?) would open a collider and bias
  the effect, so the headline number stays unconditioned.

* **Secondary = R_exist-stratified, EXPLICITLY EXPLORATORY.** Restricting to the
  instances where relevant knowledge existed *before* treatment (``r_exist == True``)
  is a pre-treatment stratum, so it is causally legitimate — but the pilot is tiny,
  the stratum is thin, and we label it exploratory everywhere so a reader cannot
  promote it to a confirmatory claim.

* **A null ITT is an ALLOWED, expected outcome.** The deliverable is the *harness*
  plus the ``R_exist`` hit-rate and a directional read — NOT statistical
  significance. ``evaluate_gate`` encodes "feasibility met" and never claims
  significance; a flat ITT alone must not flip the verdict to fail. Confidence
  intervals are reported WIDE (normal-approx, widened/flagged when trials are few)
  and per-instance trial variance is surfaced so a reader can't over-read a null.

All functions here are pure and operate on plain dicts (the U6 record shape), so they
unit-test offline against the committed ``tests/fixtures/records.sample.json`` with no
agents, Docker, or network. Stats reuse :mod:`knowledge.evals.analyze_utils`
(``mean_sd``/``rate``/``fmt``) rather than reimplementing them.
"""

from __future__ import annotations

import math

from knowledge.evals.analyze_utils import fmt, mean_sd, rate

# A trial count at or below this makes the normal-approx CI untrustworthy; we still
# report a CI but flag it so the reader treats the width as a floor, not a bound.
LOW_TRIAL_FLAG = 3
# Hit-rate below this is "trivial" — too few instances carry relevant knowledge for
# the secondary stratum (or a scale-up) to be worth anything.
HIT_RATE_FLOOR = 0.10


def _ci95(mean: float | None, sd: float | None, n: int) -> tuple[float | None, float | None]:
    """Normal-approx 95% CI half-width view as (lo, hi). Wide by construction: at small
    ``n`` the SEM is large, and we additionally *flag* low-n upstream so a reader does
    not mistake a tight-looking interval for power we don't have."""
    if mean is None or sd is None or n <= 0:
        return None, None
    sem = sd / math.sqrt(n)
    half = 1.96 * sem
    return mean - half, mean + half


def _arm_stats(arm_records: list[dict]) -> dict:
    """Resolve-rate + mean cost-to-correct (with a wide CI) over one arm's records."""
    resolved = [bool(r["resolved"]) for r in arm_records]
    costs = [r["agent_cost"] for r in arm_records if r.get("agent_cost") is not None]
    cost_mean, cost_sd = mean_sd(costs)
    n = len(costs)
    lo, hi = _ci95(cost_mean, cost_sd, n)
    return {
        "n_records": len(arm_records),
        "resolve_rate": rate(resolved),
        "n_resolved": sum(resolved),
        "cost_mean": cost_mean,
        "cost_sd": cost_sd,
        "cost_ci95": [lo, hi],
        "cost_ci_low_trials": n <= LOW_TRIAL_FLAG,
        # effectiveness-aware: total agent cost spent per successfully-resolved instance
        "cost_per_resolved": (sum(costs) / sum(resolved)) if sum(resolved) else None,
        # plain average cost per attempt/record (what you pay regardless of outcome)
        "avg_cost_per_instance": cost_mean,
    }


def _per_instance_trial_variance(arm_records: list[dict]) -> dict:
    """SD of cost-to-correct across trials, *within* each instance — surfaces how noisy
    a single instance is so a reader does not read trial noise as a treatment effect."""
    by_inst: dict[str, list[float]] = {}
    for r in arm_records:
        if r.get("agent_cost") is not None:
            by_inst.setdefault(r["instance_id"], []).append(r["agent_cost"])
    out: dict[str, dict] = {}
    for inst, costs in by_inst.items():
        _, sd = mean_sd(costs)
        out[inst] = {"trials": len(costs), "cost_sd": sd}
    return out


def _split_arms(records: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    treatment = [r for r in records if r.get("arm") == "treatment"]
    control = [r for r in records if r.get("arm") == "control"]
    errors: list[str] = []
    if not treatment:
        errors.append("no treatment records")
    if not control:
        errors.append("no control records")
    return treatment, control, errors


def _stratum(records: list[dict], rexist_map: dict, want_rexist: bool) -> list[dict]:
    """Records whose instance's pre-treatment ``r_exist`` matches ``want_rexist``."""
    return [
        r for r in records
        if bool((rexist_map.get(r["instance_id"]) or {}).get("r_exist")) is want_rexist
    ]


def _itt_block(treatment: list[dict], control: list[dict]) -> dict:
    """Treatment-vs-control stats + the directional cost/resolve deltas for one set."""
    t, c = _arm_stats(treatment), _arm_stats(control)
    cost_delta = (
        None if t["cost_mean"] is None or c["cost_mean"] is None
        else t["cost_mean"] - c["cost_mean"]  # negative => treatment cheaper-to-correct
    )
    resolve_delta = (
        None if t["resolve_rate"] is None or c["resolve_rate"] is None
        else t["resolve_rate"] - c["resolve_rate"]  # positive => treatment resolves more
    )
    return {
        "treatment": t,
        "control": c,
        "cost_delta": cost_delta,
        "cost_reduced": (cost_delta is not None and cost_delta < 0),
        "resolve_delta": resolve_delta,
        "resolve_improved": (resolve_delta is not None and resolve_delta > 0),
        "treatment_trial_variance": _per_instance_trial_variance(treatment),
        "control_trial_variance": _per_instance_trial_variance(control),
    }


def _directional(itt: dict) -> bool:
    """Promising = treatment resolves more OR is cheaper-to-correct (directional only —
    NOT a significance test). Either pointing the right way counts as a signal."""
    return bool(itt.get("resolve_improved") or itt.get("cost_reduced"))


def aggregate(records: list[dict], rexist_map: dict, instances: list[dict] | None = None) -> dict:
    """Reduce per-(instance, trial, arm) records into the pilot's readouts.

    Produces (top-level keys):
      ``itt``        — primary, unconditioned treatment-vs-control over ALL records.
      ``secondary``  — EXPLORATORY, restricted to ``r_exist == True`` instances.
      ``hit_rate``   — first-class deliverable: fraction of instances with relevant
                       knowledge pre-treatment.
      ``ingestion``  — separate amortized server-side distillation cost (``None`` /
                       placeholder when the meta carries ``None`` — never fabricated).
      ``errors``     — missing-arm / missing-data issues (mirrors dogfood's aggregate).
    """
    treatment, control, errors = _split_arms(records)

    # --- primary: ITT over ALL records, regardless of R_exist ---------------- #
    itt = _itt_block(treatment, control)

    # --- secondary (exploratory): only instances where relevant knowledge existed -- #
    t_rex = _stratum(treatment, rexist_map, True)
    c_rex = _stratum(control, rexist_map, True)
    secondary = _itt_block(t_rex, c_rex)
    secondary["label"] = "exploratory"
    secondary["note"] = (
        "Pre-treatment R_exist stratum; exploratory only — underpowered, no significance claim."
    )

    # --- R_exist hit-rate: a first-class, separately-reported deliverable ----- #
    instance_ids = sorted({r["instance_id"] for r in records})
    rexist_flags = [bool((rexist_map.get(i) or {}).get("r_exist")) for i in instance_ids]
    hit_rate = {
        "n_instances": len(instance_ids),
        "n_rexist": sum(rexist_flags),
        "rate": rate(rexist_flags),
        "instances": {i: bool((rexist_map.get(i) or {}).get("r_exist")) for i in instance_ids},
    }

    # --- ingestion: SEPARATE amortized line; honest about None placeholders --- #
    ingestion = _ingestion_line(instances)

    if itt["treatment"]["resolve_rate"] is None:
        errors.append("treatment arm has no determinate resolve data")
    if itt["control"]["resolve_rate"] is None:
        errors.append("control arm has no determinate resolve data")

    return {
        "itt": itt,
        "secondary": secondary,
        "hit_rate": hit_rate,
        "ingestion": ingestion,
        "errors": errors,
    }


def _ingestion_line(instances: list[dict] | None) -> dict:
    """Sum server-side ingestion cost from per-instance meta. ``None`` costs are
    *placeholders* (not yet wired) and are reported as such — never summed as 0."""
    if not instances:
        return {"total_cost": None, "n_instances": 0, "amortized_per_instance": None,
                "cost_is_placeholder": True, "facts_ingested": 0}
    costs = [m.get("ingestion_cost") for m in instances]
    present = [c for c in costs if c is not None]
    total = sum(present) if present else None
    n = len(instances)
    return {
        "total_cost": total,
        "n_instances": n,
        "amortized_per_instance": (total / n) if (total is not None and n) else None,
        # placeholder when ANY instance carries None — we won't pretend the line is complete
        "cost_is_placeholder": len(present) < len(costs),
        "facts_ingested": sum(int(m.get("facts_ingested") or 0) for m in instances),
    }


def evaluate_gate(report: dict) -> dict:
    """Encode the pilot's Success Criteria — "feasibility met", NOT significance.

    Feasibility is met when:
      (1) the harness completed end-to-end — records present for BOTH arms across
          instances (this is the actual deliverable);
      (2) the ``R_exist`` hit-rate is non-trivial;
      (3) the directional ITT *or* exploratory secondary points the promising way.

    Critically: this NEVER claims significance, and a null/flat ITT alone does NOT
    flip the verdict to fail (criterion 3 is satisfied by the secondary or by a
    resolve-rate improvement, and the gate rests on hit-rate + direction, not p-values).
    """
    errors = list(report["errors"])
    itt, secondary, hit = report["itt"], report["secondary"], report["hit_rate"]

    harness_complete = (
        not errors
        and itt["treatment"]["n_records"] > 0
        and itt["control"]["n_records"] > 0
    )
    hit_rate_val = hit["rate"] or 0.0
    nontrivial_hit_rate = hit_rate_val >= HIT_RATE_FLOOR
    directional = _directional(itt) or _directional(secondary)

    feasible = harness_complete and nontrivial_hit_rate and directional

    reasons: list[str] = []
    if errors:
        reasons.append("data errors: " + "; ".join(errors))
    if not harness_complete and not errors:
        reasons.append("harness incomplete: missing records for an arm")
    if not nontrivial_hit_rate:
        reasons.append(f"R_exist hit-rate trivial ({fmt(hit['rate'])} < {HIT_RATE_FLOOR})")
    if not directional:
        reasons.append("no directional signal in ITT or exploratory secondary")

    return {
        "verdict": "feasibility met" if feasible else "feasibility not met",
        "significance_claimed": False,  # by construction — the pilot is underpowered
        "null_itt_allowed": True,       # a flat ITT is an expected, acceptable outcome
        "harness_complete": harness_complete,
        "hit_rate": hit["rate"],
        "nontrivial_hit_rate": nontrivial_hit_rate,
        "itt_directional": _directional(itt),
        "secondary_directional": _directional(secondary),
        "reasons": reasons if not feasible else [],
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _fmt_ci(ci: list) -> str:
    lo, hi = ci
    return f"[{fmt(lo, money=True)}, {fmt(hi, money=True)}]" if lo is not None else "[n/a]"


def _arm_lines(label: str, s: dict) -> list[str]:
    flag = "  (WIDE: low trial count)" if s["cost_ci_low_trials"] else ""
    return [
        f"  {label:<10} resolve={fmt(s['resolve_rate'])} ({s['n_resolved']}/{s['n_records']})  "
        f"cost-to-correct={fmt(s['cost_mean'], money=True)}+/-{fmt(s['cost_sd'], money=True)}  "
        f"CI95={_fmt_ci(s['cost_ci95'])}{flag}",
        f"             cost/resolved={fmt(s['cost_per_resolved'], money=True)}  "
        f"avg/instance={fmt(s['avg_cost_per_instance'], money=True)}",
    ]


def _block_lines(title: str, block: dict) -> list[str]:
    lines = [title]
    lines += _arm_lines("treatment", block["treatment"])
    lines += _arm_lines("control", block["control"])
    lines.append(
        f"  delta      cost={fmt(block['cost_delta'], money=True)} "
        f"({'cheaper' if block['cost_reduced'] else 'NOT cheaper'})  "
        f"resolve={fmt(block['resolve_delta'])} "
        f"({'improved' if block['resolve_improved'] else 'NOT improved'})"
    )
    return lines


def format_report(report: dict, gate: dict) -> str:
    itt, secondary, hit, ing = (
        report["itt"], report["secondary"], report["hit_rate"], report["ingestion"]
    )
    lines = ["=== SWE-rebench PR-knowledge pilot — feasibility readout ===",
             "(ITT primary; R_exist secondary EXPLORATORY; null ITT is an allowed outcome)", ""]

    lines += _block_lines("PRIMARY — ITT (all treatment vs all control, unconditioned):", itt)
    lines.append("")
    lines += _block_lines(
        f"SECONDARY — EXPLORATORY, R_exist==1 only ({secondary['note']}):", secondary
    )
    lines.append("")

    lines.append(
        f"R_exist hit-rate: {fmt(hit['rate'])} ({hit['n_rexist']}/{hit['n_instances']} instances "
        "carry relevant pre-treatment knowledge)"
    )
    if ing["cost_is_placeholder"]:
        lines.append(
            f"Ingestion cost (amortized): PLACEHOLDER — not all instances reported a cost "
            f"(facts_ingested total={ing['facts_ingested']})"
        )
    else:
        lines.append(
            f"Ingestion cost (amortized): total={fmt(ing['total_cost'], money=True)} "
            f"over {ing['n_instances']} instances = "
            f"{fmt(ing['amortized_per_instance'], money=True)}/instance"
        )
    lines.append("")

    lines.append(f"VERDICT: {gate['verdict']}  (no significance claimed; null ITT allowed)")
    lines.append(
        f"  harness_complete={gate['harness_complete']}  hit_rate={fmt(gate['hit_rate'])}  "
        f"itt_directional={gate['itt_directional']}  secondary_directional={gate['secondary_directional']}"
    )
    for r in gate["reasons"]:
        lines.append(f"  - {r}")
    return "\n".join(lines)
