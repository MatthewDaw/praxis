"""FL4 (R7) — the bad artifact is pinned at regression time.

Covers the ticket's acceptance condition: after a regression the pinned bundle exists in cloud
storage and re-materializes the failing tree on a machine that never held the origin repo's
objects (fixture test); a fixture secret planted in evidence text is redacted in the stored
bundle while the tree still reproduces; retention enforces the default policy (while-gating +
90 days) with expiry observable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from hooks import _praxis

from agent_factory import ingestion_api


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def failing_repo(tmp_path: Path) -> tuple[Path, str]:
    """A tiny origin repo with one commit that "fails" (the fixture failing tree)."""
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("good\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "f.txt").write_text("good\nbroken-line\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "bad")
    sha = _git(repo, "rev-parse", "HEAD")
    return repo, sha


# --------------------------------------------------------------------------- bundle reproduction

def test_repro_bundle_reproduces_the_failing_tree_on_a_clean_machine(tmp_path, failing_repo):
    """A machine that never held the origin repo's objects can still re-materialize the exact
    failing commit from the pinned bundle alone (R7's self-contained reproduction guarantee)."""
    repo, sha = failing_repo
    bundle_bytes = ingestion_api.build_repro_bundle(repo, sha)

    # A brand-new, empty directory: never touched `repo`'s objects.
    clean_machine = tmp_path / "clean-machine"
    clone_dir = ingestion_api.materialize_bundle(bundle_bytes, clean_machine)

    assert _git(clone_dir, "rev-parse", "HEAD") == sha
    assert (clone_dir / "f.txt").read_text() == "good\nbroken-line\n"


def test_repro_bundle_leaves_the_source_repo_untouched(failing_repo):
    """The throwaway ref used to bundle a bare commit sha is cleaned up — bundling must not
    permanently mutate the (possibly live) project checkout it reads from."""
    repo, sha = failing_repo
    refs_before = _git(repo, "for-each-ref")
    ingestion_api.build_repro_bundle(repo, sha)
    refs_after = _git(repo, "for-each-ref")
    assert refs_before == refs_after


# --------------------------------------------------------------------------- secret redaction

def test_pin_artifact_redacts_a_fixture_secret_in_evidence_while_bundle_still_reproduces(
    monkeypatch, tmp_path, failing_repo
):
    """A fixture secret planted in evidence text never reaches cloud storage in the clear, while
    the reproduction bundle (needed to actually re-run the failure) is left untouched."""
    repo, sha = failing_repo
    secret = "sk-LIVE1234567890ABCDEFGHIJKLMNOPQ"
    evidence = f"pytest failed: connection refused using API_KEY={secret}\nAssertionError at f.txt:2"

    written = {}

    def fake_request(method, path, *, body=None, space=None, snapshot=None, **kw):
        written.update(method=method, path=path, body=body, space=space, snapshot=snapshot)
        return {"id": "artifact-1", "action": "added"}

    monkeypatch.setattr(_praxis, "_request", fake_request)
    monkeypatch.setattr(_praxis, "ensure_space", lambda *a, **kw: a[0])

    result = ingestion_api.pin_artifact(
        project="failure-learning-loop", ticket_id="FL4-fixture", commit_sha=sha,
        repo_path=repo, diff_text="", evidence_text=evidence,
    )
    assert result["id"] == "artifact-1"

    stored_meta = written["body"]["meta"]
    assert secret not in stored_meta["evidence"]
    assert "[REDACTED]" in stored_meta["evidence"]
    assert "AssertionError at f.txt:2" in stored_meta["evidence"]  # non-secret context survives
    assert written["space"] == _praxis.FACTORY_LEARNINGS_SPACE
    assert written["snapshot"] == _praxis.FACTORY_ARTIFACTS_SNAPSHOT
    assert written["body"]["category"] == ingestion_api.ARTIFACT_CATEGORY

    # The bundle itself (what proof/re-proof actually replays) is untouched by redaction and
    # still reproduces the exact failing tree.
    bundle_bytes = ingestion_api.decode_bundle(stored_meta)
    clone_dir = ingestion_api.materialize_bundle(bundle_bytes, tmp_path / "clean-machine-2")
    assert _git(clone_dir, "rev-parse", "HEAD") == sha
    assert (clone_dir / "f.txt").read_text() == "good\nbroken-line\n"


def test_redact_secrets_is_targeted_not_blanket():
    text = "connecting with token: abcd1234efgh5678 while running the healthy suite"
    redacted = ingestion_api.redact_secrets(text)
    assert "abcd1234efgh5678" not in redacted
    assert "connecting with" in redacted
    assert "while running the healthy suite" in redacted


# --------------------------------------------------------------------------- retention policy

def test_artifact_expired_never_expires_while_gating():
    meta = {"while_gating": True, "retention_expires_at": 0.0}
    assert ingestion_api.artifact_expired(meta, now=10_000_000.0) is False


def test_artifact_expired_enforces_default_ninety_day_window():
    pinned_at = 1_000_000.0
    meta = {
        "while_gating": False,
        "retention_days": ingestion_api.DEFAULT_RETENTION_DAYS,
        "retention_expires_at": pinned_at + ingestion_api.DEFAULT_RETENTION_DAYS * 86400,
    }
    just_inside = pinned_at + ingestion_api.DEFAULT_RETENTION_DAYS * 86400 - 1
    just_outside = pinned_at + ingestion_api.DEFAULT_RETENTION_DAYS * 86400 + 1
    assert ingestion_api.artifact_expired(meta, now=just_inside) is False
    assert ingestion_api.artifact_expired(meta, now=just_outside) is True


def test_pin_artifact_records_observable_retention_metadata(monkeypatch, failing_repo):
    repo, sha = failing_repo
    written = {}
    monkeypatch.setattr(_praxis, "_request",
                        lambda method, path, *, body=None, space=None, snapshot=None, **kw:
                        written.update(body=body) or {"id": "artifact-2"})
    monkeypatch.setattr(_praxis, "ensure_space", lambda *a, **kw: a[0])

    ingestion_api.pin_artifact(project="p", ticket_id="t", commit_sha=sha, repo_path=repo)

    meta = written["body"]["meta"]
    assert meta["while_gating"] is True
    assert meta["retention_days"] == ingestion_api.DEFAULT_RETENTION_DAYS
    assert meta["retention_expires_at"] == pytest.approx(
        meta["pinned_at"] + ingestion_api.DEFAULT_RETENTION_DAYS * 86400
    )


# --------------------------------------------------------------------------- read path

def test_read_artifact_targets_the_artifacts_snapshot(monkeypatch):
    """The pinned bundle READS back from the same (space, artifacts-snapshot) it was written to —
    proof/re-proof execution's path to "the pinned bundle exists in cloud storage"."""
    captured = {}

    def fake_get_fact(cid, *, space=None, snapshot=None, **kw):
        captured.update(cid=cid, space=space, snapshot=snapshot)
        return {"id": cid, "meta": {"commit_sha": "deadbeef"}}

    monkeypatch.setattr(_praxis, "get_fact", fake_get_fact)
    fact = ingestion_api.read_artifact("artifact-1")

    assert fact["meta"]["commit_sha"] == "deadbeef"
    assert captured == {
        "cid": "artifact-1",
        "space": _praxis.FACTORY_LEARNINGS_SPACE,
        "snapshot": _praxis.FACTORY_ARTIFACTS_SNAPSHOT,
    }


# --------------------------------------------------------------------------- guards

def test_pin_artifact_requires_a_commit_sha(failing_repo):
    repo, _sha = failing_repo
    with pytest.raises(ValueError):
        ingestion_api.pin_artifact(project="p", ticket_id="t", commit_sha="", repo_path=repo)
