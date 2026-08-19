"""One command that answers "how is the campaign going".

Written because that question was asked repeatedly during the first real campaign and every
answer required hand-rolling a script against the space file over ssh. A status that is
laborious to obtain is a status nobody checks, and the two worst failures of that campaign --
a stage silently wedged, and two runs racing on one idea -- were both things a routine glance
would have caught immediately.

Reads only. It never mutates the space, so it is safe to run against a live campaign.
"""

from __future__ import annotations

from typing import Any

from knowledge.ml_registry.schema import IDEA, MODEL, TERMINAL_TRIAL_STATUSES, TRIAL
from knowledge.ml_registry.write_path import RegistrySpace

#: Verdict statuses an idea can carry, ordered so a report reads worst-to-best consistently.
IDEA_STATUSES = ("adopted", "rejected", "parked", "voided", "superseded")


def campaign_status(space: RegistrySpace, model_id: str) -> dict[str, Any]:
    """Everything worth knowing about a campaign, in one read-only pass.

    Surfaces the two conditions that are otherwise invisible: an IN-FLIGHT trial (which blocks
    its idea and means a run is either alive or died without resolving), and the ratchet count
    (which silently approaches a rollback of the last adoption).
    """
    model = space.get(model_id)
    if model is None or model.category != MODEL:
        raise KeyError(f"model {model_id!r} was never registered")

    ideas = [f for f in space.list_facts(IDEA) if f.meta.get("model_id") == model_id]
    trials = [f for f in space.list_facts(TRIAL) if f.meta.get("model_id") == model_id]
    in_flight = [t for t in trials
                 if str(t.meta.get("status", "")) not in TERMINAL_TRIAL_STATUSES]

    by_status: dict[str, list[str]] = {}
    for idea in ideas:
        st = str(idea.meta.get("status") or "untried")
        by_status.setdefault(st, []).append(str(idea.meta.get("id") or idea.id))

    return {
        "model_id": model_id,
        "metric": model.meta.get("metric"),
        "direction": model.meta.get("direction"),
        "baseline": model.meta.get("baseline"),
        "previous_baseline": model.meta.get("previous_baseline"),
        "noise_floor": model.meta.get("noise_floor"),
        "baseline_throughput": model.meta.get("baseline_throughput"),
        "void_throughput_fraction": model.meta.get("void_throughput_fraction", 0.05),
        "ideas_total": len(ideas),
        "ideas_by_status": {k: sorted(v) for k, v in sorted(by_status.items())},
        "trials_total": len(trials),
        # A trial in flight blocks its idea. If no process is running, that run died without
        # resolving and the idea is wedged until it is superseded.
        "trials_in_flight": [{"trial_id": t.id, "idea_id": t.meta.get("idea_id"),
                              "commit": t.meta.get("commit"),
                              "status": t.meta.get("status")} for t in in_flight],
        # Approaches a rollback of the last adoption silently. Worth seeing before it fires.
        "ratchet_count": model.meta.get("ratchet_count", 0),
        "rejection_streak_ideas": list(model.meta.get("rejection_streak_ideas") or []),
        "ratchet_resets": list(model.meta.get("ratchet_resets") or []),
        "diagnoses": diagnose(space, model_id),
    }


def format_status(status: dict[str, Any]) -> str:
    """Human-readable rendering. The JSON is for programs; this is for the question being asked."""
    lines = [
        f"model      {status['model_id']}  metric={status['metric']} ({status['direction']})",
        f"baseline   {status['baseline']}  floor={status['noise_floor']}"
        f"  void_ref={status['baseline_throughput']}"
        f"  speed_void={status.get('void_throughput_fraction', 0.05)}",
        f"ideas      {status['ideas_total']} total, {status['trials_total']} trial(s) run",
    ]
    for st in list(IDEA_STATUSES) + ["untried"]:
        ids = status["ideas_by_status"].get(st)
        if ids:
            lines.append(f"  {st:<11} {len(ids):3d}  {', '.join(ids)}")

    if status["trials_in_flight"]:
        lines.append("")
        lines.append("IN FLIGHT (blocks its idea; if nothing is running, this run died):")
        for t in status["trials_in_flight"]:
            lines.append(f"  {t['trial_id']}  idea={t['idea_id']}  commit={t['commit']}")

    for d in status.get("diagnoses", []):
        lines.append("")
        lines.append(f"{d['severity'].upper()}: {d['kind']}")
        lines.append(f"  {d['detail']}")

    ratchet = status["ratchet_count"] or 0
    if ratchet:
        lines.append("")
        lines.append(f"RATCHET    {ratchet}/3 -- at 3 the last adoption is rolled back"
                     f"  ({', '.join(status['rejection_streak_ideas'])})")
    if status["ratchet_resets"]:
        lines.append(f"  cleared {len(status['ratchet_resets'])} time(s) at stage boundaries")
    return "\n".join(lines)

#: Two voids of the same KIND is not bad luck, it is a mis-set harness. One is noise; the second
#: says the setting that produced it will keep producing it, and a loop that merely re-runs will
#: reproduce the same truncation until CLOSE_VOID_LIMIT ends the campaign with no explanation.
REPEATED_VOID_THRESHOLD = 2


def diagnose(space: RegistrySpace, model_id: str) -> list[dict[str, str]]:
    """Actionable diagnoses a supervising loop can act on WITHOUT a human reading the ledger.

    A void is a decision to re-run. That is correct once, and wrong the moment the reason is
    something re-running cannot change. The registry already knows the difference -- it recorded
    `void_reason` -- but nothing turned that into advice, so an autonomous loop would re-run a
    truncated arm, truncate it again, and void again until the campaign closed on the void limit
    having explained nothing.

    Observed on the first campaign to run expensive arms: the two most costly architectures in the
    backlog -- a graph model and a widened recurrent one -- both exceeded a wall clock tuned for
    heads that finish in a third of the time. Both were voided. Re-running either would have
    reproduced the truncation exactly. That is a selection effect, not a measurement: a budget
    tuned to cheap heads silently removes precisely the arms that might beat them, and the
    campaign converges on "cheap models win" as an artefact of its own harness.
    """
    trials = [f for f in space.list_facts(TRIAL) if f.meta.get("model_id") == model_id]
    voided = [t for t in trials if str(t.meta.get("status")) == "voided"]

    unfair = [t for t in voided if "not a fair run" in str(t.meta.get("void_reason", ""))]
    slow = [t for t in voided if "throughput" in str(t.meta.get("void_reason", ""))]

    out: list[dict[str, str]] = []
    if len(unfair) >= REPEATED_VOID_THRESHOLD:
        out.append({
            "kind": "budget_too_small",
            "severity": "blocking",
            "detail": f"{len(unfair)} arms voided as unfair runs (truncated). RE-RUNNING WILL NOT "
                      f"HELP -- the same wall clock will truncate them again. Raise the run budget "
                      f"above the SLOWEST arm you intend to try, not the typical one. A budget "
                      f"tuned to cheap arms silently removes the expensive ones, and the campaign "
                      f"then converges on 'cheap wins' as an artefact of its own harness.",
        })
    if len(slow) >= REPEATED_VOID_THRESHOLD:
        out.append({
            "kind": "void_gate_too_tight",
            "severity": "blocking",
            "detail": f"{len(slow)} arms voided for throughput. If those arms are structurally "
                      f"slower (a richer representation, a heavier head) they can NEVER pass, and "
                      f"the gate is rejecting them on cost rather than merit. Set "
                      f"void_throughput_fraction to 0 to disable it, or reference it against the "
                      f"SLOWEST baseline run rather than the median.",
        })

    # An idea whose most recent trial voided is waiting to be re-run. Nothing else says so, and a
    # loop that treats voided as answered leaves it permanently unmeasured -- a non-answer that
    # records nothing, which is strictly worse than a rejection.
    latest: dict[str, object] = {}
    for t in trials:
        latest[str(t.meta.get("idea_id"))] = t
    needs_rerun = sorted(iid for iid, t in latest.items()
                         if str(t.meta.get("status")) == "voided")
    if needs_rerun:
        out.append({
            "kind": "awaiting_rerun",
            "severity": "info",
            "detail": f"{len(needs_rerun)} idea(s) whose latest trial voided and are still "
                      f"unmeasured: {', '.join(needs_rerun)}",
        })
    return out
