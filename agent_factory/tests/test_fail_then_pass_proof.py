"""FL5 (R6/R7) — the fail-then-pass proof engine.

Covers the ticket's acceptance condition: a drafted check that passes on the bad artifact never
activates as gating; a check failing both bad artifact and healthy reference inserts as
report_only only; the live project checkout HEAD never moves during proof (asserted here); a
flaky check failing its repeat count yields no proof; a corrupted/unreproducible pin routes to
report_only plus a flag event rather than erroring silently or blocking.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from typing import Any

import pytest
from hooks import _praxis

from agent_factory import ingestion_api


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo_with_regression(tmp_path: Path) -> dict[str, Any]:
    """One repo with a healthy commit followed by a regressing (bad) commit — the fixture surface
    every test in this file proves against. ``bad_artifact_meta`` mirrors what FL4's
    :func:`ingestion_api.pin_artifact` would have written (a decodable ``bundle_b64``)."""
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


# --------------------------------------------------------------------------- proven

def test_check_that_fails_bad_and_passes_healthy_is_proven(repo_with_regression):
    fx = repo_with_regression
    result = ingestion_api.run_fail_then_pass_proof(
        "grep -q expected-marker f.txt", bad_artifact_meta=fx["bad_artifact_meta"],
        healthy_repo_path=fx["repo"], healthy_ref=fx["healthy_sha"],
    )
    assert result["status"] == "proven"
    assert result["flag"] is False


# --------------------------------------------------------------------------- E4: vacuous pass on bad artifact

def test_check_that_passes_the_bad_artifact_is_never_proven(repo_with_regression):
    """A check discriminating on nothing (always passes) never activates as gating — the
    acceptance condition's first bullet."""
    fx = repo_with_regression
    result = ingestion_api.run_fail_then_pass_proof(
        "grep -c '' f.txt", bad_artifact_meta=fx["bad_artifact_meta"],
        healthy_repo_path=fx["repo"], healthy_ref=fx["healthy_sha"],
    )
    assert result["status"] != "proven"
    assert result["reason"] == "vacuous-pass-on-bad-artifact"


def test_redraft_budget_exhausted_on_vacuous_passes_yields_check_undraftable(repo_with_regression):
    fx = repo_with_regression
    result = ingestion_api.attempt_fail_then_pass_proof(
        ["grep -c '' f.txt"], bad_artifact_meta=fx["bad_artifact_meta"],
        healthy_repo_path=fx["repo"], healthy_ref=fx["healthy_sha"], redraft_budget=1,
    )
    assert result["status"] == ingestion_api.PROOF_CHECK_UNDRAFTABLE
    assert result["flag"] is True
    assert result["attempts"] == 1


def test_redraft_budget_finds_a_later_discriminating_candidate(repo_with_regression):
    """A vacuous first draft is redrafted (D1) — a later candidate that DOES discriminate still
    lands proven, within budget."""
    fx = repo_with_regression
    result = ingestion_api.attempt_fail_then_pass_proof(
        ["grep -c '' f.txt", "grep -q expected-marker f.txt"],
        bad_artifact_meta=fx["bad_artifact_meta"], healthy_repo_path=fx["repo"],
        healthy_ref=fx["healthy_sha"], redraft_budget=3,
    )
    assert result["status"] == "proven"
    assert result["attempts"] == 2
    assert result["run"] == "grep -q expected-marker f.txt"


# --------------------------------------------------------------------------- fails-both -> report_only

def test_check_failing_both_bad_and_healthy_inserts_as_report_only(repo_with_regression):
    fx = repo_with_regression
    result = ingestion_api.run_fail_then_pass_proof(
        "grep nonexistentpattern f.txt", bad_artifact_meta=fx["bad_artifact_meta"],
        healthy_repo_path=fx["repo"], healthy_ref=fx["healthy_sha"],
    )
    assert result["status"] == "report_only"
    assert result["reason"] == "fails-both"


# --------------------------------------------------------------------------- E6: flaky -> no proof

def test_flaky_bad_artifact_result_yields_no_proof(repo_with_regression):
    fx = repo_with_regression
    calls = {"n": 0}

    def flaky_executor(run: str, cwd: Path) -> bool:
        calls["n"] += 1
        return calls["n"] % 2 == 0  # alternates -> inconsistent across repeats

    result = ingestion_api.run_fail_then_pass_proof(
        "grep -q expected-marker f.txt", bad_artifact_meta=fx["bad_artifact_meta"],
        healthy_repo_path=fx["repo"], healthy_ref=fx["healthy_sha"], repeat_count=2,
        executor=flaky_executor,
    )
    assert result["status"] == "unproven"
    assert result["reason"] == "flaky-bad-artifact"


# --------------------------------------------------------------------------- E5: irreproducible pin

def test_corrupted_pin_routes_to_report_only_with_flag_never_raises(repo_with_regression):
    fx = repo_with_regression
    corrupted_meta = {"bundle_b64": base64.b64encode(b"not a real git bundle").decode("ascii")}
    result = ingestion_api.run_fail_then_pass_proof(
        "grep -q expected-marker f.txt", bad_artifact_meta=corrupted_meta,
        healthy_repo_path=fx["repo"], healthy_ref=fx["healthy_sha"],
    )
    assert result["status"] == "report_only"
    assert result["reason"] == "pin-irreproducible"
    assert result["flag"] is True


# --------------------------------------------------------------------------- the live checkout HEAD never moves

def test_live_checkout_head_and_refs_never_move_during_proof(repo_with_regression):
    fx = repo_with_regression
    head_before = _git(fx["repo"], "rev-parse", "HEAD")
    refs_before = _git(fx["repo"], "for-each-ref")

    ingestion_api.run_fail_then_pass_proof(
        "grep -q expected-marker f.txt", bad_artifact_meta=fx["bad_artifact_meta"],
        healthy_repo_path=fx["repo"], healthy_ref=fx["healthy_sha"],
    )

    assert _git(fx["repo"], "rev-parse", "HEAD") == head_before
    assert _git(fx["repo"], "for-each-ref") == refs_before


# --------------------------------------------------------------------------- ingest() wiring

def _authed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Who:
        ok = True
        principal = "user-1"
        detail = ""

    monkeypatch.setattr(_praxis, "whoami", lambda: _Who())


def _recording_request(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    counter = {"n": 0}

    def fake_request(method, path, *, body=None, params=None, space=None, snapshot=None, **kw):
        calls.append({"method": method, "path": path, "body": body,
                      "space": space, "snapshot": snapshot})
        if method == "POST" and path == "/insights":
            counter["n"] += 1
            return {"id": f"fake-{counter['n']}", "action": "added"}
        return {}

    monkeypatch.setattr(_praxis, "_request", fake_request)
    monkeypatch.setattr(_praxis, "ensure_space", lambda *a, **kw: a[0])
    return calls


def test_ingest_wires_the_real_proof_engine_and_gates_only_when_proven(monkeypatch, repo_with_regression):
    fx = repo_with_regression
    _authed(monkeypatch)
    calls = _recording_request(monkeypatch)

    result = ingestion_api.ingest(
        "always grep for the regression before shipping", "proj",
        drafted_run="grep -q expected-marker f.txt", channel="machine",
        bad_artifact_meta=fx["bad_artifact_meta"], healthy_repo_path=fx["repo"],
        healthy_ref=fx["healthy_sha"],
    )
    assert result["proof_status"] == "proven"
    assert result["check_id"] is not None
    check_calls = [c for c in calls if c["body"] and c["body"].get("category") == "check"]
    assert check_calls[0]["body"]["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_GATING


def test_ingest_never_gates_a_vacuous_machine_check_and_writes_no_check_after_budget(
    monkeypatch, repo_with_regression
):
    fx = repo_with_regression
    _authed(monkeypatch)
    calls = _recording_request(monkeypatch)

    result = ingestion_api.ingest(
        "a vacuous drafted check", "proj", drafted_run="grep -c '' f.txt", channel="machine",
        bad_artifact_meta=fx["bad_artifact_meta"], healthy_repo_path=fx["repo"],
        healthy_ref=fx["healthy_sha"], redraft_budget=1,
    )
    assert result["proof_status"] == ingestion_api.PROOF_CHECK_UNDRAFTABLE
    assert result["check_id"] is None
    assert not [c for c in calls if c["body"] and c["body"].get("category") == "check"]
    episodic_calls = [c for c in calls if c["body"] and c["body"].get("category") == "episodic"]
    assert episodic_calls  # the flag event
