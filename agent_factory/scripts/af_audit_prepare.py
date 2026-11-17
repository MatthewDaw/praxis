"""Persist source-only audit preparation across supervisor restarts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from agent_factory.af_audit_queue import audit_queue

LIST_FIELDS = ("roles", "steps", "reuse")
TEXT_FIELDS = ("route", "fixture", "expected", "cleanup")


def validate_entries(entries, allowed_ids):
    if not isinstance(entries, list):
        raise ValueError("preparation must be a JSON array")
    plans = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("preparation entries must be objects")
        case_id = entry.get("id")
        if not isinstance(case_id, str) or case_id not in allowed_ids or case_id in plans:
            raise ValueError("unexpected or duplicate preparation ID")
        for field in TEXT_FIELDS:
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ValueError(f"{case_id} requires {field}")
        for field in LIST_FIELDS:
            values = entry.get(field)
            if (not isinstance(values, list) or (not values and field != "reuse")
                    or any(not isinstance(value, str) or not value.strip() for value in values)):
                raise ValueError(f"{case_id} requires valid {field}")
        plans[case_id] = entry
    return plans


def preparation_path(config):
    return Path(config["runtime"]) / "state" / f"audit-preparation-{config['audit_run']}.json"


def save_preparation(path, scope, plans):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        try:
            json.dump({"scope": scope, "plans": list(plans.values())}, handle, indent=2)
            handle.flush()
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)


def prepare(config, env, current, run_turn):
    """Return (plan path, ready). Only browser work waits for preparation."""
    meta = current.get("run_meta", {})
    if "inventory_ids" not in meta:
        return None, True
    originals = set(meta["inventory_ids"])
    outstanding = {case["case_id"]: case for case in current["cases"]
                   if case["case_id"] in originals and case["initial"]["status"] not in ("passed", "failed")}
    if not outstanding:
        return None, True
    scope = dict(org=env["PRAXIS_ORG"], project=config["project"], run=config["audit_run"],
                 build=meta["initial_build"])
    path = preparation_path(config)
    plans = {}
    if path.exists():
        saved = json.loads(path.read_text())
        if saved.get("scope") == scope:
            plans = validate_entries(saved["plans"], originals)
    missing = outstanding.keys() - plans.keys()
    if not missing:
        return path, True

    queue = audit_queue(current)
    focuses = {case_id: focus for batch in queue["batches"]
               for case_id, focus in batch.get("case_focus", {}).items()}
    linked = {}
    for case in current["cases"]:
        parent = current.get("coverage_links", {}).get(case["case_id"])
        if parent in missing and case["initial"]["status"] in ("passed", "failed"):
            linked.setdefault(parent, []).append(case)
    # Preserve related scenarios in the same preparation turn where possible.
    order = [i for batch in queue["batches"] for i in batch["case_ids"] if i in missing]
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    attempts = 0
    with ThreadPoolExecutor(max_workers=3) as pool:
        while order:
            batch_size = 24
            shards = [order[i:i + batch_size] for i in range(0, min(len(order), 3 * batch_size), batch_size)]
            pending = [pool.submit(prepare_shard, config, env, run_turn, path.parent, scope, [
                {**outstanding[i], "prior_evidence": focuses.get(i, {}), "linked_evidence": linked.get(i, [])}
                for i in ids
            ]) for ids in shards]
            completed = set()
            for future in as_completed(pending):
                additions = future.result()
                if additions:
                    plans.update(additions)
                    completed.update(additions)
                    save_preparation(path, scope, plans)
                    print(json.dumps({"event": "audit_preparation_saved",
                                      "prepared": len(outstanding.keys() & plans.keys()),
                                      "remaining": len(outstanding.keys() - plans.keys())}), flush=True)
            if completed:
                order = [i for i in order if i not in completed]
                attempts = 0
            else:
                attempts += 1
                if attempts >= config["audit_no_progress_limit"]:
                    return path, False
    return path, True


def prepare_shard(config, env, run_turn, directory, scope, cases):
    with TemporaryDirectory(prefix="audit-preparation-", dir=directory) as temp:
        request = Path(temp) / "scenarios.json"
        result = Path(temp) / "plans.json"
        request.write_text(json.dumps({"scope": scope, "cases": cases}))
        prompt = (
            f"Prepare only the audit scenarios in {request}. Use local source and prior evidence. "
            "This is source-only planning. Do not open a browser, call the deployed app, "
            "run the scenarios, write verdicts, or modify app code. Reuse prior preparation and "
            "verified evidence within the tested role and fixture scope. "
            "For each case describe only remaining checks. Read the project assignment for fixture context at "
            f"{config['audit_prompt_file']}. Write a JSON array to {result} with one object per case. "
            "Fields: id, route, roles (array), fixture, steps (array), expected, cleanup, reuse (array). "
            "Cite local source for uncertain actions, identify missing fixture prerequisites, and keep secrets out. "
            "Use a concrete route and specific normal product actions wherever the source supports them."
        )
        run_turn(config, env, prompt, "prepare")
        if result.is_file():
            try:
                return validate_entries(json.loads(result.read_text()), {case["case_id"] for case in cases})
            except ValueError:
                return {}
        return {}
