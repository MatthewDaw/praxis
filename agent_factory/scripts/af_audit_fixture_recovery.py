"""Recover blocked smoke fixtures using operator-configured project commands."""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from af_audit_prerequisites import execute


SELF_RECOVERY = (
    "Local fixture recovery. Read the existing blocker and recover the prerequisite yourself "
    "inside this run's disposable instance. Use real app operations or seed synthetic local "
    "records, accounts, or emulator state as needed. Verify readiness and exercise the "
    "remaining behavior before recording a verdict. If it stays blocked, record the exact "
    "prerequisite and the recovery you attempted."
)
MAX_PROVISION_ATTEMPTS = 3
BROWSER_BLOCKERS = ("Local browser interrupted:", "Local runtime interrupted: Chromium")
BROWSER_HANDOFF = (
    "Fresh Chromium preflight passed. Reuse retained evidence. Resume the remaining checks "
    "in a new browser context. For a service-error simulation, mount the app before intercepting "
    "the specific local API request and remove the interception before testing recovery."
)
BROWSER_REVISION = "fresh-browser-v1"


def browser_interrupted(result):
    if result.get("blocker", "").startswith(BROWSER_BLOCKERS):
        return True
    return any("ERR_INSUFFICIENT_RESOURCES" in str(observation)
               for field in ("console", "network") for observation in (result.get(field) or []))


def local_recovery_enabled(config, current):
    target = urlsplit(current["run_meta"].get("target", ""))
    return (config.get("audit_smoke_local_recovery", False)
            and target.scheme in ("http", "https") and target.hostname in ("127.0.0.1", "::1", "localhost"))


def offer_local_recovery(config, current, retries, deferred, save):
    """Offer one worker-led recovery alongside normal work."""
    if not local_recovery_enabled(config, current):
        return retries
    from agent_factory.af_audit_queue import smoke_retry_due
    meta = current["run_meta"]
    done = {case["case_id"] for case in current["cases"] if case["initial"]["status"] in ("passed", "failed")}
    reserved = {key for key, assignment in meta.get("scheduling_assignments", {}).items()
                if assignment.get("status") == "reserved"}
    excluded = done | deferred | reserved
    eligible = [case_id for case_id in meta["inventory_ids"] if case_id not in excluded]
    dispatch = {**current, "run_meta": {**meta, "smoke_retries": retries}}
    if any(smoke_retry_due(dispatch, case_id) for case_id in eligible):
        return retries
    for case_id in eligible:
        record = current.get("smoke", {}).get(case_id, {})
        result = record.get("result", {})
        if result.get("status") != "blocked":
            continue
        if config.get("audit_browser_probe") and browser_interrupted(result):
            continue
        attempted = [record, *record.get("prior_attempts", [])]
        if any(entry.get("retry") == SELF_RECOVERY for entry in attempted):
            continue
        updated = {**retries, case_id: SELF_RECOVERY}
        save({"smoke_retries": updated})
        return updated
    return retries


def recover_fixtures(config, current, env, save, *, run=execute, now=time.time):
    meta = current["run_meta"]
    states = dict(meta.get("smoke_fixture_recovery", {}))
    retries = dict(meta.get("smoke_retries", {}))
    smoke = current.get("smoke", {})
    inventory = set(meta["inventory_ids"])
    already_done = {c["case_id"] for c in current["cases"] if c["initial"]["status"] in ("passed", "failed")}
    waiting = False
    providers = list(config.get("audit_smoke_fixture_providers", {}).items())
    browser_probe = config.get("audit_browser_probe") if local_recovery_enabled(config, current) else None
    if browser_probe:
        providers.insert(0, (":browser-preflight", {
            "probe": browser_probe, "handoff": BROWSER_HANDOFF,
        }))
    for key, provider in providers:
        browser = key == ":browser-preflight"
        prefixes = tuple(provider.get("blocker_prefixes", []))
        candidates = {}
        for case_id in inventory.intersection(provider.get("case_ids", inventory)):
            record = smoke.get(case_id, {})
            result = record.get("result", {})
            if case_id in already_done or result.get("status") != "blocked":
                continue
            blocker = result.get("blocker", "")
            if browser_probe:
                interrupted = browser_interrupted(result)
                if browser != interrupted:
                    continue
            if browser and any(BROWSER_REVISION in (entry.get("retry") or "")
                               for entry in [record, *record.get("prior_attempts", [])]):
                continue
            if browser or not prefixes or blocker.startswith(prefixes):
                candidates[case_id] = record
        if not candidates:
            continue
        previous = states.get(key, {})
        state = dict(previous)
        clock = now()
        if clock < state.get("next_probe", 0):
            waiting |= not state.get("revision") and state.get("attempts", 0) < MAX_PROVISION_ATTEMPTS
            continue
        state["next_probe"] = clock + 300
        if browser:
            observation = run(provider["probe"], env, config["worktree"], 30)
            revision = BROWSER_REVISION if observation.get("ok") else None
        else:
            revision = observe(provider["probe"], config, env, run)
        if revision is None and state.get("attempts", 0) < MAX_PROVISION_ATTEMPTS:
            state.pop("revision", None)
            state["attempts"] = state.get("attempts", 0) + 1
            if not browser:
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
        else:
            state.pop("revision", None)
            waiting |= state.get("attempts", 0) < MAX_PROVISION_ATTEMPTS
        states[key] = state
    changes = {}
    if states != meta.get("smoke_fixture_recovery", {}):
        changes["smoke_fixture_recovery"] = states
    if retries != meta.get("smoke_retries", {}):
        changes["smoke_retries"] = retries
    if changes:
        save(changes)
    return retries, waiting


def observe(command, config, env, run):
    result = run(command, env, config["worktree"], 90)
    output = result.get("output", {})
    if result.get("ok") and isinstance(output, dict) and output.get("available") is True:
        revision = output.get("revision")
        if isinstance(revision, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", revision):
            return revision
    return None
