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


def smoke_retry_due(status, case_id):
    record = status.get("smoke", {}).get(case_id, {})
    retry = status.get("run_meta", {}).get("smoke_retries", {}).get(case_id)
    return bool(retry and record.get("result", {}).get("status") == "blocked" and retry != record.get("retry"))


def smoke_progress(status):
    """Present independent smoke outcomes, reusing original terminal verdicts."""
    meta = status.get("run_meta", {})
    originals = set(meta.get("inventory_ids", []))
    smoke = status.get("smoke", {})
    counts = Counter()
    for case in status.get("cases", []):
        case_id = case["case_id"]
        if case_id not in originals:
            continue
        if case["initial"]["status"] in ("passed", "failed"):
            counts["reused_full"] += 1
        elif case_id in smoke and not smoke_retry_due(status, case_id):
            counts[smoke[case_id]["result"]["status"]] += 1
        else:
            counts["pending"] += 1
    return {"total": len(originals), **{k: counts[k] for k in ("passed", "failed", "blocked", "reused_full", "pending")}}


def audit_queue(status, *, now=None, final_check_ids=()):
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
    metrics = sweep_queue(status, batches, final_check_ids=final_check_ids)
    total = len(originals)
    completed = counts["passed"] + counts["failed"]
    progress = {k: counts[k] for k in ("passed", "failed", "assigned", "retry", "pending", "blocked")}
    progress.update(total=total, completed=completed, remaining=total-completed,
                    percent_complete=round(100*completed/total, 1) if total else 0)
    times = sorted(terminal_times)[-20:]
    eta = {"timed_originals": len(times), "runnable_hours": None, "full_audit_hours": None}
    metrics["original_verdicts_per_hour"] = None
    # A burst of bulk writes is insufficient to estimate the pace of actual testing.
    if total == completed:
        eta.update(runnable_hours=0, full_audit_hours=0)
    elif len(times) >= 4 and times[-1] - times[0] >= 300:
        end = (now or datetime.now(timezone.utc)).timestamp()
        seconds_per_case = (max(end, times[-1]) - times[0]) / (len(times)-1)
        metrics["original_verdicts_per_hour"] = round(3600 / seconds_per_case, 2)
        remaining = total - completed - counts["blocked"]
        eta["runnable_hours"] = ceil(seconds_per_case*remaining/360)/10
        if not counts["blocked"] and dispatch:
            eta["full_audit_hours"] = eta["runnable_hours"]
    return {"state": "mapped", "phase": "initial_audit", "build": meta["initial_build"],
            "progress": progress, "smoke_progress": smoke_progress(status), "eta": eta, "sweep_metrics": metrics, "batches": batches,
            "completed_cases": completed_cases,
            "additional_cases": [c["case_id"] for c in status["cases"] if c["case_id"] not in set_originals]}


def sweep_queue(status, visits, *, final_check_ids=()):
    """Index recorded assertions by case and profile without inheriting verdicts."""
    by_case = {}
    remaining = {}
    seen = set()
    revisits = 0
    sweeps = sorted(status.get("sweeps", []), key=lambda s: (s["started"], s["recording_started"]))
    for sweep in sweeps:
        scope = sweep["scope"]
        for route in sweep["routes"]:
            key = (route, scope["role"], scope["sport"], scope["viewport"], scope["fixtureState"])
            revisits += key in seen
            seen.add(key)
        remaining.update(sweep["remaining"])
        for check in sweep["checks"]:
            key = (check["id"], check["route"], scope["role"], scope["sport"], scope["viewport"], scope["fixtureState"])
            by_case.setdefault(check["caseId"], {})[key] = (scope, check, sweep["artifact"]["sha256"])
    final_check_ids = set(final_check_ids)
    for visit in visits:
        focus = {}
        for case_id in visit["case_ids"]:
            details = {}
            if case_id in remaining:
                details["remaining"] = remaining[case_id]
            else:
                legacy = status["run_meta"].get("queue_followups", {}).get(case_id, {})
                if legacy.get("remaining_steps"):
                    details["prior_followup"] = legacy["remaining_steps"]
            for scope, check, sha in by_case.get(case_id, {}).values():
                if case_id in final_check_ids and check["verified"]:
                    details.setdefault("verified_final_checkpoints", []).append(dict(
                        id=check["id"], action=check["action"], route=check["route"],
                        tested_scope=scope, evidence=sha))
                    continue
                shared = check.get("equivalentRoles", [])
                for role in visit["roles"]:
                    # Unspecified inventory scope requires the worker to compare
                    # the concrete visit scope before accepting checkpoint reuse.
                    if not check["verified"] or role not in (scope["role"], *shared):
                        continue
                    if any(visit[k] and visit[k] != scope[v] for k, v in
                           (("sport", "sport"), ("viewport", "viewport"), ("fixture_state", "fixtureState"))):
                        continue
                    details.setdefault("verified_checks", []).append(dict(
                        id=check["id"], role=role, tested_scope=scope, route=check["route"],
                        evidence=sha, equivalence_basis=check.get("equivalenceBasis") if role != scope["role"] else None))
            if details:
                focus[case_id] = details
        if focus:
            visit["case_focus"] = focus
        near = [i for i in visit["case_ids"] if i in remaining and len(remaining[i]) <= 1]
        if near:
            visit["close_first"] = near
    return {"recorded_sweeps": len(sweeps), "distinct_scoped_pages": len(seen), "scoped_revisits": revisits}


def next_visits(status, queue, limit=6):
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
            visits[key] = {k: v for k, v in batch.items() if k not in ("case_ids", "state", "case_focus", "close_first")}
            visits[key]["case_ids"] = []
        visits[key]["case_ids"].extend(batch["case_ids"])
        if batch.get("case_focus"):
            visits[key].setdefault("case_focus", {}).update(batch["case_focus"])
        if batch.get("close_first"):
            visits[key].setdefault("close_first", []).extend(batch["close_first"])
    ordered = sorted(visits.values(), key=lambda visit: (
        not bool(visit.get("close_first")), not visit["route"].startswith("/"), evidence[visit["route"]]
    ))
    first_by_route = {}
    followups = []
    for visit in ordered:
        route = visit["route"]
        if route in first_by_route or not route.startswith("/"):
            followups.append(visit)
        else:
            first_by_route[route] = visit
    return [*first_by_route.values(), *followups][:limit]
