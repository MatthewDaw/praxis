"""FL7 (R5/R6/R15/E11) — the merger's single entry point for a merger-driven regression.

Covers the ticket's acceptance condition:
  * a merger-driven regression produces a lesson whose evidence links the regression detail
    (:func:`ingestion_api.regress_with_ingestion`, the machine-strict "same motion" call);
  * a proof exceeding the merge-time budget moves to background: the regressed ticket's rerun
    does not claim until the background proof lands, while sibling tickets are unaffected;
  * Praxis unreachable halts regression and ingestion TOGETHER, loudly, with no file fallback
    (the exception propagates; nothing partial is written to disk).
"""

from __future__ import annotations

import base64
import subprocess
import time
from pathlib import Path
from typing import Any, NoReturn

import pytest
from hooks import _praxis
from hooks import _ticket_state as ts

from agent_factory import ingestion_api


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo_with_regression(tmp_path: Path) -> dict[str, Any]:
    """Mirrors ``test_fail_then_pass_proof.repo_with_regression`` — one repo with a healthy
    commit followed by a regressing (bad) commit, used to drive the REAL proof engine."""
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("expected-marker\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "healthy")
    healthy_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "f.txt").write_text("regressed-value\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "bad")
    bad_sha = _git(repo, "rev-parse", "HEAD")
    bundle_bytes = ingestion_api.build_repro_bundle(repo, bad_sha)
    bad_artifact_meta = {"bundle_b64": base64.b64encode(bundle_bytes).decode("ascii")}
    return {"repo": repo, "healthy_sha": healthy_sha, "bad_sha": bad_sha,
            "bad_artifact_meta": bad_artifact_meta}


# `ingestion_api` imports `_praxis` via the `hooks` namespace package (``from hooks import
# _praxis``), while `_ticket_state` imports it bare (``import _praxis``, resolved off the
# hooks/ directory PYTHONPATH puts directly on sys.path) — two DISTINCT module objects for the
# same file. A double that must be visible to code going through either import path (this file
# calls both `ingestion_api.regress_with_ingestion` AND `ts.claim` directly) has to patch both.
_PRAXIS_MODULES = (_praxis, ts._praxis)


def _authed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Stub:
        ok = True
        principal = "user-1"
        detail = ""
    for mod in _PRAXIS_MODULES:
        monkeypatch.setattr(mod, "whoami", lambda: _Stub())


def _fake_backend(
    monkeypatch: pytest.MonkeyPatch, *, tickets: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """A ``_praxis`` double recording writes and serving/mutating an in-memory ticket table
    keyed by id (each a ``{"meta": {...}}`` dict) — enough for regress/get_fact/write_build_state
    round-tripping without a live Praxis backend. Patched onto BOTH module objects (see
    ``_PRAXIS_MODULES``) so it is visible however the code under test reached ``_praxis``."""
    calls: list[dict[str, Any]] = []
    tickets = tickets if tickets is not None else {}
    counter = {"n": 0}

    def fake_request(method: str, path: str, *, body: dict[str, Any] | None = None,
                     params: dict[str, Any] | None = None, space: str | None = None,
                     snapshot: str | None = None, **kw: Any) -> dict[str, Any]:
        calls.append({"method": method, "path": path, "body": body,
                      "space": space, "snapshot": snapshot})
        if method == "POST" and path == "/insights":
            counter["n"] += 1
            return {"id": f"fake-{counter['n']}", "action": "added"}
        return {}

    def fake_get_fact(cid: str, **kw: Any) -> dict[str, Any] | None:
        return tickets.get(cid)

    def fake_regress_requirements(project: str, ids: list[str],
                                  detail: dict[str, Any] | None = None,
                                  **kw: Any) -> dict[str, Any]:
        detail = detail or {}
        for tid in ids:
            t = tickets.setdefault(tid, {"id": tid, "meta": {}})
            patch = (detail.get(tid) if isinstance(detail, dict) and tid in detail else detail) or {}
            t["meta"].update(patch)
            t["meta"]["build_state"] = "incomplete"
        calls.append({"method": "REGRESS", "path": "/requirements/regress",
                     "body": {"ids": list(ids), "detail": detail}})
        return {"count": len(ids)}

    def fake_write_build_state(cid: str, meta_dict: dict[str, Any], owner: str | None = None,
                               **kw: Any) -> dict[str, Any]:
        t = tickets.setdefault(cid, {"id": cid, "meta": {}})
        for k, v in meta_dict.items():
            if v is None:
                t["meta"].pop(k, None)
            else:
                t["meta"][k] = v
        calls.append({"method": "BUILD_STATE", "path": f"/requirements/{cid}/build-state",
                     "body": dict(meta_dict)})
        return t

    def fake_claim_requirement(cid: str, owner: str, ttl: int, **kw: Any) -> dict[str, Any]:
        t = tickets.setdefault(cid, {"id": cid, "meta": {}})
        t["meta"]["claim_owner"] = owner
        t["meta"]["build_state"] = "in_progress"
        return t

    for mod in _PRAXIS_MODULES:
        monkeypatch.setattr(mod, "_request", fake_request)
        monkeypatch.setattr(mod, "ensure_space", lambda *a, **kw: a[0])
        monkeypatch.setattr(mod, "get_fact", fake_get_fact)
        monkeypatch.setattr(mod, "regress_requirements", fake_regress_requirements)
        monkeypatch.setattr(mod, "write_build_state", fake_write_build_state)
        monkeypatch.setattr(mod, "claim_requirement", fake_claim_requirement)
    return calls, tickets


# --------------------------------------------------------------------------- R5: regression w/o ingestion is illegal

def test_regress_with_ingestion_requires_at_least_one_ticket_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _authed(monkeypatch)
    _fake_backend(monkeypatch)
    with pytest.raises(ValueError, match="ticket id"):
        ingestion_api.regress_with_ingestion("proj", [], "a lesson")


# --------------------------------------------------------------------------- R5: same-motion, evidence-linked

def test_merger_regression_fires_ingestion_in_the_same_motion(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real proof engine wired (no bad_artifact_meta/healthy_repo_path) -> falls straight
    through to the ordinary same-motion :func:`ingestion_api.ingest` behaviour: a lesson lands
    AND the ticket is regressed with evidence (lesson_id) linking the regression detail."""
    _authed(monkeypatch)
    calls, tickets = _fake_backend(monkeypatch, tickets={"FL99": {"id": "FL99", "meta": {}}})

    result = ingestion_api.regress_with_ingestion("proj", ["FL99"], "the merger caught a regression")

    assert result["lesson_id"] is not None
    regress_calls = [c for c in calls if c["method"] == "REGRESS"]
    assert regress_calls, "regression must fire in the same motion as ingestion"
    detail = regress_calls[-1]["body"]["detail"]["FL99"]["regression_detail"]
    assert detail[-1]["lesson_id"] == result["lesson_id"]


# --------------------------------------------------------------------------- R6/R15: within-budget proof

def test_proof_within_budget_completes_synchronously_and_gates(
    monkeypatch: pytest.MonkeyPatch, repo_with_regression: dict[str, Any],
) -> None:
    _authed(monkeypatch)
    fx = repo_with_regression
    _calls, tickets = _fake_backend(monkeypatch, tickets={"FL99": {"id": "FL99", "meta": {}}})

    result = ingestion_api.regress_with_ingestion(
        "proj", ["FL99"], "grep check regression",
        drafted_run="grep -q expected-marker f.txt",
        bad_artifact_meta=fx["bad_artifact_meta"], healthy_repo_path=fx["repo"],
        healthy_ref=fx["healthy_sha"], merge_budget_s=30,
    )

    assert result["proof_status"] == "proven"
    assert result["check_id"] is not None
    assert not tickets["FL99"]["meta"].get(ts.M_PROOF_PENDING)


# --------------------------------------------------------------------------- R15: over-budget -> background

def test_proof_exceeding_budget_backgrounds_and_holds_only_that_ticket(
    monkeypatch: pytest.MonkeyPatch, repo_with_regression: dict[str, Any],
) -> None:
    _authed(monkeypatch)
    fx = repo_with_regression
    _calls, tickets = _fake_backend(monkeypatch, tickets={
        "FL99": {"id": "FL99", "meta": {}},
        "FL100": {"id": "FL100", "meta": {}},  # a sibling ticket, untouched by this regression
    })

    def slow_executor(run: str, cwd: Path) -> bool:
        time.sleep(0.3)
        return subprocess.run(run, shell=True, cwd=str(cwd), check=False).returncode == 0

    result = ingestion_api.regress_with_ingestion(
        "proj", ["FL99"], "slow proof regression",
        drafted_run="grep -q expected-marker f.txt",
        bad_artifact_meta=fx["bad_artifact_meta"], healthy_repo_path=fx["repo"],
        healthy_ref=fx["healthy_sha"], merge_budget_s=0.01,
        proof_executor=slow_executor,
    )

    # Immediate return: no gating verdict yet, background continuation, merge/other tickets proceed.
    assert result.get("background") is True
    assert result["proof_status"] == "pending"

    # THIS ticket is regressed immediately (merge does not wait) but marked proof_pending so its
    # rerun cannot claim; the SIBLING ticket is entirely unaffected.
    assert tickets["FL99"]["meta"].get(ts.M_PROOF_PENDING) is True
    assert ts.claim("FL99", "worker-1", ref=None) is False
    assert not tickets["FL100"]["meta"].get(ts.M_PROOF_PENDING)

    future = result["future"]
    final = future.result(timeout=10)  # wait for the background proof to actually land
    assert final["proof_status"] == "proven"

    # Once the background proof lands, the pending marker clears and the ticket becomes claimable
    # again (subject to the ordinary lease rules the fake backend does not otherwise restrict).
    assert not tickets["FL99"]["meta"].get(ts.M_PROOF_PENDING)


# --------------------------------------------------------------------------- E11: Praxis-down halts both, no fallback

def test_praxis_unreachable_halts_regression_and_ingestion_together(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _authed(monkeypatch)

    def boom(*a: Any, **kw: Any) -> NoReturn:
        raise _praxis.PraxisUnreachable("Praxis GET /candidates/FL99 -> HTTP 503: down")

    for mod in _PRAXIS_MODULES:
        monkeypatch.setattr(mod, "_request", boom)
        monkeypatch.setattr(mod, "ensure_space", lambda *a, **kw: a[0])
        monkeypatch.setattr(mod, "get_fact", boom)
        monkeypatch.setattr(mod, "regress_requirements", boom)
        monkeypatch.setattr(mod, "write_build_state", boom)

    before = set(tmp_path.iterdir())
    with pytest.raises(_praxis.PraxisUnreachable):
        ingestion_api.regress_with_ingestion("proj", ["FL99"], "unreachable-backend regression")
    # Loudly halted, no side file dropped anywhere as a fallback.
    assert set(tmp_path.iterdir()) == before


# --------------------------------------------------------------------------- claim() honors proof_pending

def test_claim_refuses_a_ticket_with_proof_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(ts, "_meta", lambda cid, ref=None: {ts.M_PROOF_PENDING: True})
    for mod in _PRAXIS_MODULES:
        monkeypatch.setattr(mod, "claim_requirement",
                            lambda *a, **kw: calls.append("claimed") or {})
    assert ts.claim("FL99", "worker-1") is False
    assert calls == [], "a proof-pending ticket must never reach the server-side claim grant"
