"""Build a bounded execution packet from a single canonical Praxis snapshot."""
import json
from collections import defaultdict

from agent_factory.af_audit_queue import audit_queue, smoke_retry_due


def execution_packet(current, plan_path, recent, *, smoke=False):
    prepared = {p["id"]: p for p in json.loads(plan_path.read_text())["plans"]}
    cases = {case["case_id"]: case for case in current["cases"]}
    links = defaultdict(list)
    for child, parent in current.get("coverage_links", {}).items():
        case = cases.get(child)
        if case and case["initial"]["status"] in ("passed", "failed"):
            links[parent].append(case)

    visits = defaultdict(list)
    for batch in audit_queue(current)["batches"]:
        if batch["state"] not in ("pending", "retry"):
            continue
        for case_id in batch["case_ids"]:
            if smoke and case_id in current.get("smoke", {}) and not smoke_retry_due(current, case_id):
                continue
            focus = batch.get("case_focus", {}).get(case_id, {})
            p = prepared[case_id]
            roles = tuple(p["roles"][:1] if smoke else p["roles"])
            key = (p["route"], roles, batch["sport"], batch["viewport"], batch["fixture_state"])
            visits[key].append((case_id, focus))
    if not visits:
        return []

    def priority(item):
        key, members = item
        return min(case_priority(key, case_id, focus) for case_id, focus in members)

    def case_priority(key, case_id, focus):
        remaining = focus.get("remaining")
        if smoke:
            return (not smoke_retry_due(current, case_id), recent.count(case_id), not key[0].startswith("/"), ":" in key[0],
                    not bool(focus.get("verified_checks")), len(prepared[case_id]["steps"]))
        return (recent.count(case_id), not (remaining is not None and len(remaining) <= 1),
                not key[0].startswith("/"), len(key[1]), not bool(focus.get("verified_checks")))

    packets = []
    for key, members in sorted(visits.items(), key=priority):
        selected = sorted(members, key=lambda member: case_priority(key, *member))[:(6 if smoke else 3)]
        packets.append([{
            "plan": prepared[case_id],
            "inventory": cases[case_id]["scenario"],
            "current": cases[case_id]["initial"],
            "checkpoint_focus": focus,
            "linked_verdicts": links[case_id],
            **({"new_fixture": current["run_meta"]["smoke_retries"][case_id],
                "prior_smoke_blocker": current["smoke"][case_id]["result"].get("blocker")}
               if smoke_retry_due(current, case_id) else {}),
        } for case_id, focus in selected])
        if len(packets) == (6 if smoke else 2):
            break
    return packets
