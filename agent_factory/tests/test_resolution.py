"""FL10 (R17) — per-finding resolution + the CHECK-DEFEAT failure class.

Covers the ticket's acceptance condition:
  * a rerun passing check A does not stamp finding B (a sibling check's finding) resolved;
  * a check still FAILING leaves every finding untouched;
  * a fixture where the check passes but the recorded symptom persists produces a check-defeat
    record, pins the rebuilt state's artifact, demotes the check to report_only, and triggers a
    redraft attempt against the fresh artifact.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:  # same seam test_widening.py pins: bare `_praxis` must resolve to THIS
    sys.path.insert(0, _HOOKS)  # worktree's hooks/, never a stale copy shadowed via PYTHONPATH.

import pytest
from hooks import _praxis

from agent_factory import ingestion_api, resolution

FINDING_A = {"reason": "check-a symptom: derive_flight_ids raises AttributeError",
            "evidence": "AttributeError at geometry.py:42", "check_id": "check-a"}
FINDING_B = {"reason": "check-b symptom: the ingestion CLI --help crashes",
            "evidence": "IndexError: list index out of range", "check_id": "check-b"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _make_repo(tmp_path: Path, name: str, text: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text(text + "\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "commit")
    return repo


@pytest.fixture
def failing_repo(tmp_path: Path) -> Path:
    """A repo whose sole (bad) commit does NOT reproduce the grep marker any redraft candidate
    looks for — the pinned bad artifact a check-defeat's redraft must fail against."""
    return _make_repo(tmp_path, "origin", "regressed-value")


@pytest.fixture
def healthy_repo(tmp_path: Path) -> Path:
    """The healthy reference the redraft candidate must PASS against (R6's "proven" verdict)."""
    return _make_repo(tmp_path, "sibling", "expected-marker")


@pytest.fixture(autouse=True)
def _stubbed_backend(monkeypatch):
    """:func:`ingestion_api.pin_artifact`/:func:`demote_for_check_defeat`/:func:`failure_taxonomy.
    assign_class` all round-trip through Praxis; stub the transport so these tests exercise the
    DECISION logic (exactly which finding resolves, whether a defeat is detected, what gets
    pinned/demoted/redrafted), not the network — same seam ``test_widening.py`` uses."""
    monkeypatch.setattr(ingestion_api, "_require_authenticated", lambda identity=None: identity or "tester")
    monkeypatch.setattr(_praxis, "ensure_space", lambda *a, **kw: a[0])
    written: list[dict[str, Any]] = []

    def fake_request(method, path, *, body=None, space=None, snapshot=None, **kw):
        written.append({"method": method, "path": path, "body": body, "space": space,
                        "snapshot": snapshot})
        return {"id": f"fake-{len(written)}", "action": "added"}

    monkeypatch.setattr(_praxis, "_request", fake_request)
    monkeypatch.setattr(_praxis, "facts_by", lambda *a, **kw: [])  # no existing failure classes

    patch_calls: list[dict[str, Any]] = []

    def fake_patch_check(check_id, project, build_patch, *, identity=None):
        patch = build_patch({"id": check_id, "meta": {"enforcement_state": "gating"}}) \
            if callable(build_patch) else dict(build_patch)
        patch_calls.append({"check_id": check_id, "project": project, "patch": patch})
        return {"id": check_id, "meta": patch}

    monkeypatch.setattr(ingestion_api, "_patch_check", fake_patch_check)

    def fake_read_artifact(artifact_id):
        for w in written:
            if w["path"] == "/insights" and (w["body"] or {}).get("category") == "artifact":
                return {"id": artifact_id, "meta": w["body"]["meta"]}
        return {"id": artifact_id, "meta": {}}

    monkeypatch.setattr(ingestion_api, "read_artifact", fake_read_artifact)
    return {"written": written, "patch_calls": patch_calls}


# --------------------------------------------------------------------------- R17: per-finding scoping

def test_check_a_passing_does_not_stamp_check_b_finding_resolved(_stubbed_backend, failing_repo):
    meta = {"regression_detail": [dict(FINDING_A), dict(FINDING_B)]}
    result = resolution.resolve_or_defeat(
        meta, "check-a", check_passed=True, symptom_present=False,
        project="failure-learning-loop", ticket_id="FL10-fixture",
        commit_sha=_git(failing_repo, "rev-parse", "HEAD"), repo_path=failing_repo,
    )
    assert result["status"] == "resolved"
    by_check = {d["check_id"]: d for d in result["regression_detail"]}
    assert by_check["check-a"]["resolved"] is True
    assert "resolved" not in by_check["check-b"] or by_check["check-b"]["resolved"] is False


def test_check_still_failing_leaves_every_finding_open(_stubbed_backend, failing_repo):
    meta = {"regression_detail": [dict(FINDING_A), dict(FINDING_B)]}
    result = resolution.resolve_or_defeat(
        meta, "check-a", check_passed=False, symptom_present=False,
        project="failure-learning-loop", ticket_id="FL10-fixture",
        commit_sha=_git(failing_repo, "rev-parse", "HEAD"), repo_path=failing_repo,
    )
    assert result["status"] == "unresolved"
    assert not any(d.get("resolved") for d in result["regression_detail"])


# --------------------------------------------------------------------------- R17: check-defeat

def test_check_passed_but_symptom_persists_produces_check_defeat(_stubbed_backend, failing_repo,
                                                                  healthy_repo):
    meta = {"regression_detail": [dict(FINDING_A)]}
    sha = _git(failing_repo, "rev-parse", "HEAD")
    result = resolution.resolve_or_defeat(
        meta, "check-a", check_passed=True, symptom_present=True,
        project="failure-learning-loop", ticket_id="FL10-fixture",
        commit_sha=sha, repo_path=failing_repo, healthy_repo_path=healthy_repo,
        run_candidates=["grep -q expected-marker f.txt"],
    )
    assert result["status"] == "check-defeat"
    # the finding stays OPEN — the symptom is still there, nothing about it is resolved
    assert not any(d.get("resolved") for d in result["regression_detail"])

    # pinned the rebuilt state's artifact (FL4)
    artifact_writes = [w for w in _stubbed_backend["written"]
                       if (w["body"] or {}).get("category") == "artifact"]
    assert len(artifact_writes) == 1
    assert artifact_writes[0]["body"]["meta"]["commit_sha"] == sha

    # classified into the taxonomy, feeding R3
    assert result["classification"]["action"] == "minted"
    class_writes = [w for w in _stubbed_backend["written"]
                    if (w["body"] or {}).get("category") == "failure-class"]
    assert class_writes and class_writes[0]["body"]["meta"]["kind"] == resolution.CHECK_DEFEAT_CLASS_KIND

    # demoted GATING -> REPORT_ONLY and flagged
    demote_calls = [c for c in _stubbed_backend["patch_calls"] if c["check_id"] == "check-a"]
    assert demote_calls
    assert demote_calls[0]["patch"]["enforcement_state"] == ingestion_api.STATE_REPORT_ONLY
    flag_writes = [w for w in _stubbed_backend["written"]
                  if (w["body"] or {}).get("category") == "flag"]
    assert flag_writes and flag_writes[0]["body"]["meta"]["kind"] == ingestion_api.FLAG_KIND_CHECK_DEFEAT

    # a machine-strict redraft was attempted against the fresh pin, and it reproduces (proven:
    # the fixture repo IS the healthy reference here, so the redrafted check passes on it)
    assert result["redraft"] is not None
    assert result["redraft"]["status"] == "proven"


def test_check_defeat_with_no_run_candidates_skips_redraft_but_still_defeats(_stubbed_backend, failing_repo):
    meta = {"regression_detail": [dict(FINDING_A)]}
    result = resolution.resolve_or_defeat(
        meta, "check-a", check_passed=True, symptom_present=True,
        project="failure-learning-loop", ticket_id="FL10-fixture",
        commit_sha=_git(failing_repo, "rev-parse", "HEAD"), repo_path=failing_repo,
    )
    assert result["status"] == "check-defeat"
    assert result["redraft"] is None


# --------------------------------------------------------------------------- resolve_findings_for_check

def test_resolve_findings_for_check_scopes_to_one_check_id():
    meta = {"regression_detail": [dict(FINDING_A), dict(FINDING_B)]}
    updated = resolution.resolve_findings_for_check(meta, "check-a", resolved_by="verifier")
    by_check = {d["check_id"]: d for d in updated}
    assert by_check["check-a"]["resolved"] is True
    assert by_check["check-a"]["resolved_by"] == "verifier"
    assert by_check["check-b"].get("resolved", False) is False
