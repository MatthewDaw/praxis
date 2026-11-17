"""Runner-neutral audit ledger. Praxis owns state; JSON inputs/exports are artifacts.

Only one coordinator advances a run. Browser workers record distinct cases; the
coordinator waits for them to stop before freezing or advancing a phase.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from hooks import _praxis
from agent_factory.ingestion_api import redact_secrets
from agent_factory.af_audit_queue import audit_queue

CATEGORY = "audit-fix"
TERMINAL = {"passed", "failed", "blocked"}


def require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def verify_artifact(artifact: dict) -> None:
    """Verify bytes on the browser host before accepting an evidence reference.

    HTTPS exports require their retained local evidence_path; arbitrary network
    fetching would leak credentials or reach internal services. No other scheme
    is silently trusted. A publisher may export the same bytes after recording.
    """
    uri = urlsplit(artifact["uri"])
    require(uri.scheme in {"file", "https"} and not uri.username and not uri.password,
            "evidence URI must use file or HTTPS without credentials")
    if uri.scheme == "file":
        require(uri.netloc in {"", "localhost"}, "remote file evidence is not supported")
        path = Path(url2pathname(uri.path))
    else:
        require(nonempty(artifact.get("evidence_path")), "HTTPS evidence requires retained local evidence_path")
        path = Path(artifact["evidence_path"])
    require(path.is_file(), "evidence file does not exist on the recording host")
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    require(value.hexdigest() == artifact["sha256"], "evidence file SHA256 mismatch")


class Audit:
    def __init__(self, project: str, run: str, client: Any = _praxis):
        require(bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,95}", project)), "invalid project slug")
        require(bool(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}", run)), "invalid run slug")
        self.project, self.run, self.client = project, run, client
        self.scope = {"space": project, "snapshot": f"audit-{run}"}

    def facts(self) -> list[dict]:
        return self.client.facts_by(category=CATEGORY, meta={"audit_run": self.run}, state="any", **self.scope)

    def one(self, key: str) -> dict:
        matches = [f for f in self.facts() if f["meta"].get("audit_key") == key]
        require(len(matches) == 1, f"expected exactly one {key} fact; found {len(matches)}")
        return matches[0]

    def create(self, key: str, data: dict) -> dict:
        old = [f for f in self.facts() if f["meta"].get("audit_key") == key]
        require(len(old) <= 1, f"duplicate {key}; reconcile in Praxis")
        if old:
            return old[0]
        meta = {"audit_run": self.run, "audit_key": key, **data}
        self.client._request("POST", "/insights", body={
            "insight": f"Audit {self.project}/{self.run}: {key}", "category": CATEGORY,
            "raw": True, "meta": meta,
        }, **self.scope)
        return self.one(key)

    def patch(self, fact: dict, data: dict) -> None:
        self.client.patch_meta(fact["id"], data, **self.scope)

    def init(self, inventory: dict, target: str, build: str) -> dict:
        parsed = urlsplit(target)
        require(parsed.scheme in {"http", "https"} and parsed.netloc and not parsed.username and not parsed.password,
                "target must be an HTTP(S) URL without credentials")
        require(nonempty(build), "deployed build identity required")
        scenarios = inventory.get("scenarios")
        require(isinstance(scenarios, list) and bool(scenarios), "nonempty scenarios required")
        ids = [c.get("id") for c in scenarios]
        require(all(nonempty(x) for x in ids) and len(ids) == len(set(ids)), "unique nonempty case IDs required")
        self.client.ensure_space(self.project, name=self.project)
        run = self.create("run", {"phase": "audit", "target": target, "initial_build": build,
                                  "inventory_hash": digest(inventory), "inventory_ids": ids, "case_ids": ids})
        meta = run["meta"]
        require(meta["inventory_hash"] == digest(inventory) and meta["target"] == target and meta["initial_build"] == build,
                "run already exists with different inventory, target, or build")
        # Retry after interrupted init creates only missing cases.
        for case in scenarios:
            self.create("case:" + case["id"], {"case_id": case["id"], "scenario": case,
                                                "initial": {"status": "not_run"}})
        return self.status()

    def reconcile(self, inventory: dict, links: dict) -> dict:
        """Attach partial evidence to the original inventory without changing verdicts."""
        fact = self.one("run")
        run = fact["meta"]
        require(run["phase"] == "audit", "reconcile before freezing the audit")
        require(run["inventory_hash"] == digest(inventory), "original inventory hash differs")
        ids = [case["id"] for case in inventory["scenarios"]]
        require(set(ids) <= set(run["case_ids"]), "original inventory cases missing")
        merged = {**run.get("coverage_links", {}), **links}
        for child, parent in merged.items():
            require(child in run["case_ids"] and child not in ids and parent in ids,
                    "coverage link requires an added case and an original inventory ID")
        self.patch(fact, {"inventory_ids": ids, "coverage_links": merged})
        return self.status()

    @staticmethod
    def execution_key(scenario: dict) -> tuple:
        fields = ("parent_id", "behavior_id", "role", "sport", "viewport")
        return tuple(re.sub(r"\s+", " ", str(scenario.get("required_" + field,
                     scenario.get(field, ""))).strip()).casefold() for field in fields)

    def check(self, case_id: str) -> dict:
        """Call before browser execution; return prior evidence for covered cases."""
        return self.check_many([case_id])[0]

    def check_many(self, case_ids: list[str]) -> list[dict]:
        status = self.status()
        return [self._checked_case(status, case_id) for case_id in case_ids]

    @staticmethod
    def _checked_case(status: dict, case_id: str) -> dict:
        phase = status["phase"]
        require(phase in {"audit", "reaudit"}, "browser checks only run during audit phases")
        matches = [c for c in status["cases"] + status["regression_neighbors"] if c["case_id"] == case_id]
        require(len(matches) == 1, "case not found or ambiguous")
        case = matches[0]
        run = status["run_meta"]
        if phase == "reaudit":
            require(case_id in run["failed_ids"] or case in status["regression_neighbors"],
                    "re-audit limited to frozen failures and linked regression neighbors")
        result = case.get("initial" if phase == "audit" else "reaudit", {})
        build = run["initial_build"] if phase == "audit" else run["deployment"]["build"]
        reuse = result.get("build") == build and result.get("status") in {"passed", "failed"}
        linked = [c for c in status["cases"]
                  if status["coverage_links"].get(c["case_id"]) == case_id
                  and c["initial"].get("build") == build
                  and c["initial"]["status"] in {"passed", "failed"}]
        return {"case_id": case_id, "build": build, "reuse": reuse, "result": result,
                "linked_evidence": [{"case_id": c["case_id"], "scenario": c["scenario"],
                                     "result": c["initial"]} for c in linked]}

    def add_case(self, case: dict) -> dict:
        run = self.one("run")
        phase = run["meta"]["phase"]
        require(phase in {"audit", "reaudit"}, "inventory frozen outside audit phases")
        require(nonempty(case.get("id")), "case ID required")
        if phase == "reaudit":
            require(case.get("regression_for") in run["meta"]["failed_ids"], "regression neighbor must link a frozen failure")
            require(case["id"] not in run["meta"]["case_ids"], "neighbor cannot replace an original case")
            fact = self.create("neighbor:" + case["id"], {"case_id": case["id"], "scenario": case})
            require(fact["meta"]["scenario"] == case, "neighbor ID already used")
            return fact
        if run["meta"].get("inventory_ids"):
            original = run["meta"]["inventory_ids"]
            require(case["id"] not in original, "use existing original case ID")
            require(case.get("parent_id") in original or
                    (not case.get("parent_id") and nonempty(case.get("discovered_control"))),
                    "link an original scenario or describe the uncovered control")
            key = self.execution_key(case)
            require(all(key[1:]), "behavior_id, role, sport (or n/a), and viewport required")
            for existing in self._cases(run["meta"]):
                if existing["case_id"] != case["id"] and self.execution_key(existing["scenario"]) == key:
                    raise ValueError(f"duplicate behavior tuple; reuse {existing['case_id']}")
        fact = self.create("case:" + case["id"], {"case_id": case["id"], "scenario": case,
                                                   "initial": {"status": "not_run"}})
        require(fact["meta"]["scenario"] == case, "case ID already used by another scenario")
        ids = run["meta"]["case_ids"]
        if case["id"] not in ids:
            self.patch(run, {"case_ids": ids + [case["id"]]})
        return fact

    def validate_result(self, result: dict, build: str, target: str, scenario: dict) -> None:
        require(result.get("status") in TERMINAL, "status must be passed, failed, or blocked")
        require(result.get("build") == build, "deployed build drift; start a new audit run")
        for field in ("role", "route", "expected", "actual"):
            require(nonempty(result.get(field)), f"{field} required")
        route = urlsplit(result["route"])
        origin = urlsplit(target)
        require(not route.username and not route.password, "result route cannot contain credentials")
        if route.scheme or route.netloc:
            require((route.scheme, route.hostname, route.port or (443 if route.scheme == "https" else 80))
                    == (origin.scheme, origin.hostname, origin.port or (443 if origin.scheme == "https" else 80)),
                    "result route is outside the audited origin")
        else:
            require(result["route"].startswith("/") and not result["route"].startswith("//"), "result route must be an absolute path")
        for field in ("role", "sport", "viewport"):
            expected = scenario.get("required_" + field, scenario.get(field))
            if nonempty(expected):
                require(result.get(field) == expected, f"result {field} differs from required case tuple")
        allowed_roles = scenario.get("roles")
        if isinstance(allowed_roles, list) and allowed_roles:
            require(result["role"] in allowed_roles, "result role is outside scenario roles; expand role-specific cases first")
        require(isinstance(result.get("steps"), list) and bool(result["steps"])
                and all(nonempty(x) for x in result["steps"]), "concrete executed steps required")
        evidence = result.get("evidence")
        require(isinstance(evidence, list) and bool(evidence), "evidence references required")
        for artifact in evidence:
            require(isinstance(artifact, dict) and nonempty(artifact.get("uri"))
                    and bool(re.fullmatch(r"[a-f0-9]{64}", artifact.get("sha256", ""))),
                    "each evidence reference needs URI and SHA256")
            verify_artifact(artifact)
        if result["status"] == "blocked":
            require(nonempty(result.get("blocker")), "blocked requires explicit unmet prerequisite")
        else:
            require(nonempty(result.get("viewport")), "viewport required")
            require(isinstance(result.get("console"), list) and isinstance(result.get("network"), list),
                    "console and network observations required (empty arrays mean inspected and clean)")
        require(nonempty(result.get("cleanup")), "cleanup ledger reference or explicit no mutations required")

    def record(self, case_id: str, result: dict, *, receipt: bool = False) -> dict:
        run = self.one("run")["meta"]
        phase = run["phase"]
        require(phase in {"audit", "reaudit"}, "case evidence only writable during audit or reaudit")
        matches = [f for f in self.facts() if f["meta"].get("audit_key") in {"case:" + case_id, "neighbor:" + case_id}]
        require(len(matches) == 1, "case not found or ambiguous")
        fact = matches[0]
        slot = "initial" if phase == "audit" else "reaudit"
        if phase == "reaudit":
            require(case_id in run["failed_ids"] or fact["meta"]["audit_key"].startswith("neighbor:"),
                    "re-audit limited to frozen initial failures and linked regression neighbors")
        self.validate_result(result, run["initial_build"] if slot == "initial" else run["deployment"]["build"],
                             run["target"], fact["meta"]["scenario"])
        old = fact["meta"].get(slot, {})
        require(slot == "reaudit" or old.get("status", "not_run") == "not_run" or old.get("status") == "blocked" or old == result,
                "recorded result is immutable; use a new run for another execution")
        acknowledgment = {"case_id": case_id, "phase": phase, "status": result["status"], "build": result["build"]}
        if old == result:
            return acknowledgment if receipt else self.status()
        if slot == "reaudit" and old.get("build") == result["build"] and old.get("status") in {"passed", "failed"}:
            require(nonempty(result.get("rerun_reason")), "same-build re-audit requires rerun_reason")
        # Credentials must remain in private fixture bundles, never the evidence ledger.
        require(redact_secrets(json.dumps(result)) == json.dumps(result), "redact credentials before recording evidence")
        changes = {slot: copy.deepcopy(result), slot + "_recorded_at": datetime.now(timezone.utc).isoformat()}
        if slot == "reaudit":
            changes["reaudit_attempts"] = fact["meta"].get("reaudit_attempts", []) + [copy.deepcopy(result)]
        self.patch(fact, changes)
        return acknowledgment if receipt else self.status()

    def record_sweep(self, path: Path) -> dict:
        """Attach the browser's action evidence in one write. Verdicts remain explicit."""
        from time import time_ns
        recording = time_ns() // 1_000_000
        raw = path.read_bytes()
        report = json.loads(raw)
        runs = self.client.facts_by(category=CATEGORY, meta={"audit_run": self.run, "audit_key": "run"},
                                    state="any", **self.scope)
        require(len(runs) == 1, "expected one audit run")
        run = runs[0]["meta"]
        require(run["phase"] == "audit", "sweep checkpoints require an open initial audit")
        require(report.get("build") == run["initial_build"] and
                urlsplit(report.get("origin", ""))[:2] == urlsplit(run["target"])[:2], "sweep target or build differs")
        scope = {key: report.get(key) for key in ("role", "sport", "viewport", "fixtureState")}
        require(all(nonempty(v) for v in scope.values()), "explicit role, sport, viewport and fixtureState required")
        start, browser, elapsed = (report.get(k) for k in ("batchStartedAt", "browserStartedAt", "elapsedMs"))
        require(all(type(t) is int for t in (start, browser, elapsed)) and
                0 < start <= browser <= recording and elapsed >= 0, "invalid sweep timing")
        checks = []
        routes = []
        for visit in report["visits"]:
            route = visit["path"]
            require(nonempty(route) and route.startswith("/") and not route.startswith("//")
                    and not urlsplit(route).query, "use a local route without query parameters")
            routes.append(route)
            for action in visit["actions"]:
                if "caseId" not in action:
                    continue
                require(action["caseId"] in run["case_ids"], "unknown sweep case")
                keys = ("id", "action", "expected", "actual")
                require(all(nonempty(action.get(k)) for k in keys) and type(action.get("verified")) is bool,
                        "incomplete action evidence")
                entry = {k: action[k] for k in (*keys, "caseId", "verified")}
                roles = action.get("equivalentRoles", [])
                boundary = action.get("permissionBoundary", True)
                require(type(boundary) is bool and isinstance(roles, list) and all(nonempty(r) for r in roles),
                        "invalid profile scope")
                if roles:
                    require(action["verified"] and not boundary and nonempty(action.get("equivalenceBasis")),
                            "shared checks need verified behavior and an explicit equivalence basis")
                    entry.update(equivalentRoles=roles, equivalenceBasis=action["equivalenceBasis"])
                checks.append(dict(entry, route=route, permissionBoundary=boundary))
        remaining = report.get("remaining", {})
        checked_ids = {a["caseId"] for a in checks}
        require(checks and isinstance(remaining, dict) and set(remaining) <= checked_ids and
                all(isinstance(items, list) and all(nonempty(v) for v in items) for items in remaining.values()),
                "remaining steps must reference exercised scenarios")
        sha = hashlib.sha256(raw).hexdigest()
        key = "sweep:" + sha
        data = dict(scope=scope, checks=checks, remaining=remaining, routes=routes,
                    artifact={"uri": path.resolve().as_uri(), "sha256": sha},
                    started=start, browser_ms=elapsed, planning_ms=browser-start, recording_started=recording)
        require(redact_secrets(json.dumps(data)) == json.dumps(data), "redact sweep evidence")
        existing = self.client.facts_by(category=CATEGORY, meta={"audit_run": self.run, "audit_key": key},
                                        state="any", **self.scope)
        if existing:
            saved = existing[0]["meta"]
        else:
            self.client._request("POST", "/insights", body={
                "insight": f"Audit {self.project}/{self.run}: {key}", "category": CATEGORY, "raw": True,
                "meta": {"audit_run": self.run, "audit_key": key, **data},
            }, **self.scope)
            saved = data
        end = time_ns() // 1_000_000
        return dict(key=key, actions=len(checks), recording_ms=end-recording,
                    planning_ms=saved["planning_ms"], browser_ms=saved["browser_ms"],
                    end_to_end_ms=end-start)

    def advance(self, gate: dict) -> dict:
        fact = self.one("run")
        run = fact["meta"]
        phase = run["phase"]
        cases = self._cases(run)
        require(gate.get("observed_build") == run.get("deployment", {}).get("build", run["initial_build"]),
                "deployment identity missing or changed")
        if phase == "audit":
            identity = run.get("identity_prerequisite", {})
            if identity.get("status") == "available":
                affected = set(identity.get("affected_original_ids", []))
                stale = [c["case_id"] for c in cases if c["case_id"] in affected
                         and c["initial"].get("status") == "blocked"
                         and not (c["initial"].get("prerequisite_revision") == (identity.get("revision") or "available")
                                  and isinstance(c["initial"].get("blocking_prerequisites"), list)
                                  and c["initial"]["blocking_prerequisites"]
                                  and all(nonempty(v) for v in c["initial"]["blocking_prerequisites"])
                                  and "browser-identity" not in c["initial"]["blocking_prerequisites"])]
                deferred = identity.get("external_deferred", {})
                groups = run.get("audit_prerequisites", {})
                for case in cases:
                    result = case["initial"]
                    saved = deferred.get(case["case_id"])
                    if saved and result.get("status") == "blocked" and saved.get("result_fingerprint") == digest(result):
                        dependencies = result.get("blocking_prerequisites", [])
                        observed = digest({dep: groups.get(dep, {}) for dep in dependencies})
                        if saved.get("dependency_fingerprint") != observed:
                            stale.append(case["case_id"])
                require(not stale, "reconsider previously blocked originals after prerequisite changes: " + ", ".join(stale))
            require(all(c["initial"]["status"] in TERMINAL for c in cases), "unexecuted cases remain")
            require(gate.get("discovery_reconciled") is True and nonempty(gate.get("discovery_evidence")),
                    "reconcile runtime controls against inventory before freezing")
            failed = sorted(c["case_id"] for c in cases if c["initial"]["status"] == "failed")
            frozen = [{"id": c["case_id"], "result": c["initial"]} for c in cases]
            self.patch(fact, {"phase": "fix", "failed_ids": failed, "initial_report_hash": digest(frozen),
                              "discovery_evidence": gate["discovery_evidence"], "frozen_at": datetime.now(timezone.utc).isoformat()})
        elif phase == "fix":
            fixes = gate.get("fixes", {})
            require(set(fixes) == set(run["failed_ids"]), "fix evidence must cover every frozen failure")
            require(all(isinstance(v, dict) and nonempty(v.get("commit")) and nonempty(v.get("validation")) for v in fixes.values()),
                    "each fix needs commit and regression validation evidence")
            self.patch(fact, {"phase": "deploy", "fixes": fixes})
        elif phase == "deploy":
            deployment = gate.get("deployment", {})
            require(nonempty(deployment.get("build")) and nonempty(deployment.get("evidence"))
                    and deployment.get("healthy") is True and deployment.get("migrations_verified") is True,
                    "deployment needs exact build, health/migration verification, and evidence")
            require(deployment.get("target") == run["target"], "deployment target differs from audited environment")
            self.patch(fact, {"phase": "reaudit", "deployment": deployment})
        elif phase == "reaudit":
            if gate.get("retry_fixes") is True:
                require(any(c.get("reaudit", {}).get("status") == "failed" for c in cases)
                        or any(n["meta"].get("reaudit", {}).get("status") == "failed" for n in self.facts()
                               if n["meta"]["audit_key"].startswith("neighbor:")), "no failed re-audit to repair")
                self.patch(fact, {"phase": "fix", "deployment_history": run.get("deployment_history", []) + [run["deployment"]]})
                return self.status()
            neighbors = [f["meta"] for f in self.facts() if f["meta"]["audit_key"].startswith("neighbor:")]
            require(all(n.get("reaudit", {}).get("status") == "passed"
                        and n["reaudit"]["build"] == run["deployment"]["build"] for n in neighbors),
                    "adjacent regression controls must pass current deployment")
            require(all(c.get("reaudit", {}).get("status") == "passed" and c["reaudit"]["build"] == run["deployment"]["build"]
                        for c in cases if c["case_id"] in run["failed_ids"]),
                    "failed cases must pass deployed re-audit")
            require(gate.get("cleanup_verified") is True and nonempty(gate.get("cleanup_evidence")), "verified cleanup required")
            self.patch(fact, {"phase": "partial" if any(c["initial"]["status"] == "blocked" for c in cases) else "complete",
                              "cleanup_evidence": gate["cleanup_evidence"]})
        else:
            raise ValueError(f"cannot advance phase {phase}")
        return self.status()

    def _cases(self, run: dict, facts=None) -> list[dict]:
        if facts is None:
            facts = self.facts()
        cases = [f["meta"] for f in facts if f["meta"]["audit_key"].startswith("case:")]
        require(len(cases) == len(run["case_ids"]) and {c["case_id"] for c in cases} == set(run["case_ids"]),
                "inventory import incomplete or inconsistent")
        return sorted(cases, key=lambda c: c["case_id"])

    def status(self) -> dict:
        facts = self.facts()
        runs = [f["meta"] for f in facts if f["meta"]["audit_key"] == "run"]
        require(len(runs) == 1, f"expected exactly one run fact; found {len(runs)}")
        run = runs[0]
        cases = self._cases(run, facts)
        counts = {s: sum(c["initial"]["status"] == s for c in cases) for s in ["not_run", *sorted(TERMINAL)]}
        original = set(run.get("inventory_ids", []))
        links = dict(run.get("coverage_links", {}))
        for case in cases:
            parent = case["scenario"].get("parent_id")
            if parent in original:
                links[case["case_id"]] = parent
        with_evidence = {links[c["case_id"]] for c in cases
                         if c["case_id"] in links and c["initial"]["status"] in {"passed", "failed"}}
        sweeps = [f["meta"] for f in facts if f["meta"]["audit_key"].startswith("sweep:")]
        with_evidence.update(check["caseId"] for sweep in sweeps for check in sweep["checks"])
        inventory_counts = dict.fromkeys(["untouched", "partial", "blocked", "passed", "failed"], 0)
        for case in cases:
            if case["case_id"] in original:
                state = case["initial"]["status"]
                if state == "not_run":
                    state = "partial" if case["case_id"] in with_evidence else "untouched"
                inventory_counts[state] += 1
        return {"project": self.project, "run": self.run, "phase": run["phase"], "counts": counts,
                "inventory_counts": inventory_counts, "added_cases": len(cases) - len(original) if original else None,
                "coverage_links": links,
                "sweeps": sweeps,
                "full_coverage": counts["blocked"] == 0 and counts["not_run"] == 0,
                "failed_ids": run.get("failed_ids", []), "run_meta": run, "cases": cases,
                "regression_neighbors": [f["meta"] for f in facts if f["meta"]["audit_key"].startswith("neighbor:")]}

    def report(self) -> str:
        status = self.status()
        run = status["run_meta"]
        lines = [f"# Audit {self.project}/{self.run}", "", f"Phase: {status['phase']}",
                 f"Original inventory: {json.dumps(status['inventory_counts'], sort_keys=True) if run.get('inventory_ids') else 'awaiting reconciliation'}",
                 f"All cases: {json.dumps(status['counts'], sort_keys=True)}", "",
                 "Blocked cases are untested; they are never counted as passes.", "",
                 f"Target: {run['target']}", f"Initial build: {run['initial_build']}", ""]
        for item in [*status["cases"], *status["regression_neighbors"]]:
            lines.extend([f"## {item['case_id']}: {item['scenario'].get('title', '')}", ""])
            if item["scenario"].get("regression_for"):
                lines.extend([f"Adjacent regression for: {item['scenario']['regression_for']}", ""])
            for slot, result in [("Initial", item.get("initial", {})),
                                 *[(f"Re-audit attempt {i + 1}", r) for i, r in enumerate(item.get("reaudit_attempts", []))]]:
                if not result:
                    continue
                lines.extend([f"### {slot}: {result.get('status', 'not_run')}", "",
                              f"Role: {result.get('role', 'not executed')} · Build: {result.get('build', 'unobserved')}",
                              f"Expected: {result.get('expected', item['scenario'].get('expected', ''))}",
                              f"Actual: {result.get('actual', 'Not executed')}", ""])
                if result.get("blocker"):
                    lines.extend([f"Blocker: {result['blocker']}", ""])
                lines.extend(f"- {step}" for step in result.get("steps", []))
                lines.append("")
                lines.extend(f"- Evidence: {a['uri']} (SHA256 {a['sha256']})" for a in result.get("evidence", []))
                lines.extend(["", f"Cleanup: {result.get('cleanup', 'not executed')}", ""])
        lines.extend(["## Deployment and cleanup", "", "```json",
                      json.dumps({"deployments": run.get("deployment_history", []) + ([run["deployment"]] if "deployment" in run else []),
                                  "cleanup_evidence": run.get("cleanup_evidence"), "fixes": run.get("fixes", {})}, indent=2),
                      "```", ""])
        return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--org", required=True, help="expected Praxis organization; mismatches fail closed")
    parser.add_argument("--run", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--input", required=True)
    init.add_argument("--target", required=True)
    init.add_argument("--build", required=True)
    add = sub.add_parser("add-case")
    add.add_argument("--input", required=True)
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--input", required=True, help="JSON object with original inventory and links")
    check = sub.add_parser("check")
    check.add_argument("--case", required=True, nargs="+", help="one or more case IDs sharing a single ledger read")
    record = sub.add_parser("record")
    record.add_argument("--receipt", action="store_true", help="return only the accepted case verdict")
    record.add_argument("--case", required=True)
    record.add_argument("--input", required=True)
    sub.add_parser("record-sweep").add_argument("--input", required=True)
    advance = sub.add_parser("advance")
    advance.add_argument("--input", required=True)
    sub.add_parser("status")
    sub.add_parser("report")
    sub.add_parser("queue", help="ordered initial audit batches, original coverage and throughput estimate")
    args = parser.parse_args(argv)
    try:
        _praxis.assert_org_matches(args.org)
        audit = Audit(args.project, args.run)
        data = None
        if hasattr(args, "input") and args.command != "record-sweep":
            with open(args.input, encoding="utf-8") as handle:
                data = json.load(handle)
        if args.command == "init":
            output = audit.init(data, args.target, args.build)
        elif args.command == "reconcile":
            output = audit.reconcile(data["inventory"], data["links"])
        elif args.command == "check":
            checks = audit.check_many(args.case)
            output = checks[0] if len(checks) == 1 else checks
        elif args.command == "record-sweep":
            output = audit.record_sweep(Path(args.input))
        elif args.command == "record":
            output = audit.record(args.case, data, receipt=args.receipt)
        elif args.command == "advance":
            output = audit.advance(data)
        elif args.command == "add-case":
            output = audit.add_case(data)
        elif args.command == "queue":
            output = audit_queue(audit.status())
        elif args.command == "report":
            print(audit.report())
            return 0
        else:
            output = audit.status()
        print(json.dumps(output, indent=2))
        return 0
    except (ValueError, OSError, _praxis.PraxisUnreachable) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
