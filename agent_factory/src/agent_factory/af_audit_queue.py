"""Build the ordered work queue from the registered inventory and current verdicts."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from math import ceil


def _context(scenario):
    routes = scenario.get("routes") or []
    roles = scenario.get("roles") or []
    return (
        scenario.get("primary_route") or scenario.get("route") or next(iter(routes), ""),
        tuple(sorted(roles)) if roles else (scenario.get("required_role") or scenario.get("role") or "",),
        scenario.get("required_sport") or scenario.get("sport") or "",
        scenario.get("required_viewport") or scenario.get("viewport") or "",
        scenario.get("fixture_state") or "",
    )


def audit_queue(status, *, now=None):
    """Read-only view. The persisted original IDs remain the progress denominator."""
    meta = status["run_meta"]
    originals = meta.get("inventory_ids")
    if originals is None:
        return {"state": "awaiting_inventory_reconciliation"}
    by_id = {case["case_id"]: case for case in status["cases"]}
    set_originals = set(originals)
    if len(set_originals) != len(originals) or any(i not in by_id for i in originals):
        raise ValueError("original inventory incomplete")
    dispatch = meta.get("queue_deferrals", {})
    held = set(dispatch.get("identity", [])) | set(dispatch.get("external", []))
    reserved = {i for i, a in meta.get("scheduling_assignments", {}).items() if a.get("status") == "reserved"}
    counts = Counter()
    groups = {}
    completed_cases = []
    terminal_times = []
    for case_id in originals:
        case = by_id[case_id]
        verdict = case["initial"]["status"]
        if verdict in ("passed", "failed"):
            counts[verdict] += 1
            completed_cases.append({"case_id": case_id, "verdict": verdict})
            if case.get("initial_recorded_at"):
                terminal_times.append(datetime.fromisoformat(case["initial_recorded_at"]).timestamp())
            continue
        if case_id in reserved:
            state = "assigned"
        elif case_id in held or (verdict == "blocked" and not dispatch):
            state = "blocked"
        elif verdict == "blocked":
            state = "retry"
        else:
            state = "pending"
        counts[state] += 1
        key = (state, *_context(case["scenario"]))
        groups.setdefault(key, []).append(case_id)

    order = {"retry": 0, "pending": 1, "assigned": 2, "blocked": 3}
    batches = []
    for (state, route, roles, sport, viewport, fixture), ids in sorted(groups.items(), key=lambda pair: order[pair[0][0]]):
        batches.append(dict(state=state, route=route, roles=list(roles), sport=sport,
                            viewport=viewport, fixture_state=fixture, case_ids=ids))
    total = len(originals)
    completed = counts["passed"] + counts["failed"]
    progress = {k: counts[k] for k in ("passed", "failed", "assigned", "retry", "pending", "blocked")}
    progress.update(total=total, completed=completed, remaining=total-completed,
                    percent_complete=round(100*completed/total, 1) if total else 0)
    times = sorted(terminal_times)[-20:]
    eta = {"timed_originals": len(times), "runnable_hours": None, "full_audit_hours": None}
    # A burst of bulk writes is insufficient to estimate the pace of actual testing.
    if total == completed:
        eta.update(runnable_hours=0, full_audit_hours=0)
    elif len(times) >= 4 and times[-1] - times[0] >= 300:
        end = (now or datetime.now(timezone.utc)).timestamp()
        seconds_per_case = (max(end, times[-1]) - times[0]) / (len(times)-1)
        remaining = total - completed - counts["blocked"]
        eta["runnable_hours"] = ceil(seconds_per_case*remaining/360)/10
        if not counts["blocked"] and dispatch:
            eta["full_audit_hours"] = eta["runnable_hours"]
    return {"state": "mapped", "phase": "initial_audit", "build": meta["initial_build"],
            "progress": progress, "eta": eta, "batches": batches,
            "completed_cases": completed_cases,
            "additional_cases": [c["case_id"] for c in status["cases"] if c["case_id"] not in set_originals]}


def next_visits(status, queue, limit=3):
    """Combine eligible cases sharing a visit and favor under-exercised surfaces."""
    cases = {c["case_id"]: c for c in status["cases"]}
    evidence = Counter()
    for case in cases.values():
        if case["initial"]["status"] == "not_run":
            continue
        parent_id = status.get("coverage_links", {}).get(case["case_id"], case["case_id"])
        parent = cases.get(parent_id, case)
        evidence[_context(parent["scenario"])[0]] += 1
    visits = {}
    for batch in queue["batches"]:
        if batch["state"] not in ("pending", "retry"):
            continue
        key = (batch["route"], tuple(batch["roles"]), batch["sport"], batch["viewport"], batch["fixture_state"])
        if key not in visits:
            visits[key] = {k: v for k, v in batch.items() if k not in ("case_ids", "state")}
            visits[key]["case_ids"] = []
        visits[key]["case_ids"].extend(batch["case_ids"])
    return sorted(visits.values(), key=lambda visit: evidence[visit["route"]])[:limit]
