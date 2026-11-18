"""Recover blocked smoke fixtures using operator-configured project commands."""
from __future__ import annotations

import re
import time

from af_audit_prerequisites import execute


def recover_fixtures(config, current, env, save, *, run=execute, now=time.time):
    meta = current["run_meta"]
    states = dict(meta.get("smoke_fixture_recovery", {}))
    retries = dict(meta.get("smoke_retries", {}))
    smoke = current.get("smoke", {})
    already_done = {c["case_id"] for c in current["cases"] if c["initial"]["status"] in ("passed", "failed")}
    for key, provider in config.get("audit_smoke_fixture_providers", {}).items():
        candidates = {
            case_id: smoke[case_id] for case_id in provider["case_ids"]
            if case_id not in already_done and smoke.get(case_id, {}).get("result", {}).get("status") == "blocked"
        }
        if not candidates:
            continue
        previous = states.get(key, {})
        state = dict(previous)
        clock = now()
        if clock < state.get("next_probe", 0):
            continue
        state["next_probe"] = clock + 300
        revision = observe(provider["probe"], config, env, run)
        if revision is None and state.get("attempts", 0) < 3:
            state["attempts"] = state.get("attempts", 0) + 1
            states[key] = state
            # Persist the attempt before provision so restarts cannot loop on failures.
            save({"smoke_fixture_recovery": states})
            run(provider["provision"], env, config["worktree"], 90)
            revision = observe(provider["probe"], config, env, run)
        if revision:
            state.update(revision=revision, attempts=0)
            instruction = f"{provider['handoff']} Verified fixture revision {revision}."
            for case_id, record in candidates.items():
                if record.get("retry") != instruction and (case_id not in retries or
                        retries[case_id] == record.get("retry")):
                    retries[case_id] = instruction
        states[key] = state
    changes = {}
    if states != meta.get("smoke_fixture_recovery", {}):
        changes["smoke_fixture_recovery"] = states
    if retries != meta.get("smoke_retries", {}):
        changes["smoke_retries"] = retries
    if changes:
        save(changes)
    return retries


def observe(command, config, env, run):
    result = run(command, env, config["worktree"], 90)
    output = result.get("output", {})
    if result.get("ok") and isinstance(output, dict) and output.get("available") is True:
        revision = output.get("revision")
        if isinstance(revision, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", revision):
            return revision
    return None
