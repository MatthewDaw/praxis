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
    }


def format_status(status: dict[str, Any]) -> str:
    """Human-readable rendering. The JSON is for programs; this is for the question being asked."""
    lines = [
        f"model      {status['model_id']}  metric={status['metric']} ({status['direction']})",
        f"baseline   {status['baseline']}  floor={status['noise_floor']}"
        f"  void_ref={status['baseline_throughput']}",
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

    ratchet = status["ratchet_count"] or 0
    if ratchet:
        lines.append("")
        lines.append(f"RATCHET    {ratchet}/3 -- at 3 the last adoption is rolled back"
                     f"  ({', '.join(status['rejection_streak_ideas'])})")
    if status["ratchet_resets"]:
        lines.append(f"  cleared {len(status['ratchet_resets'])} time(s) at stage boundaries")
    return "\n".join(lines)
