"""R40 — af-clean owns its own Praxis space, self-bootstrapping, and degrades gracefully.

``agent_factory.af_clean_space`` computes the per-target-repo space identity af-clean uses (S4:
namespaced, no cross-space read access), decides whether a run must fall back to a **degraded local
mode** when no Praxis backend is reachable/authenticated (never failing closed the way the factory
build hooks do), and applies secret redaction to anything the degraded store writes to disk.
"""

from __future__ import annotations

import json

import pytest

from agent_factory.af_clean_space import (
    backend_status,
    bootstrap_space,
    degraded_store_root,
    redact_secrets,
    repo_identity,
    space_name,
    write_degraded_marker,
)
from hooks import _praxis


def test_repo_identity_is_stable_for_the_same_repo(tmp_path):
    repo = tmp_path / "repo-a"
    repo.mkdir()
    assert repo_identity(repo) == repo_identity(repo)


def test_repo_identity_differs_across_repos_isolation(tmp_path):
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    assert repo_identity(repo_a) != repo_identity(repo_b)


def test_repo_identity_prefers_git_remote_over_path(tmp_path, monkeypatch):
    repo = tmp_path / "repo-a"
    repo.mkdir()
    (repo / ".git").mkdir()

    def fake_remote(root):
        return "git@github.com:example/thing.git"

    monkeypatch.setattr("agent_factory.af_clean_space._git_remote_url", fake_remote)
    # Two different filesystem paths that report the SAME git remote resolve to the SAME identity —
    # identity is keyed to repo identity, not to an incidental checkout path.
    other = tmp_path / "elsewhere" / "checkout"
    other.mkdir(parents=True)
    (other / ".git").mkdir()
    assert repo_identity(repo) == repo_identity(other)


def test_space_name_is_a_valid_slug_and_namespaced(tmp_path):
    repo = tmp_path / "repo-a"
    repo.mkdir()
    name = space_name(repo)
    assert name.startswith("af-clean-")
    assert name == name.lower()
    assert all(c.isalnum() or c in "-_" for c in name)


def test_backend_status_degraded_when_no_api_key(monkeypatch):
    monkeypatch.delenv("PRAXIS_API_KEY", raising=False)
    monkeypatch.delenv("PRAXIS_AUTH_DISABLED", raising=False)
    status = backend_status()
    assert status.degraded is True
    assert status.authenticated is False
    assert status.reasons  # names why, never a silent flag


def test_degraded_store_root_lives_outside_the_target_repo(tmp_path):
    repo = tmp_path / "target-repo"
    repo.mkdir()
    store = degraded_store_root(repo)
    assert repo not in store.parents and store != repo


def test_degraded_store_root_isolated_per_repo(tmp_path):
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    assert degraded_store_root(repo_a) != degraded_store_root(repo_b)


def test_redact_secrets_masks_common_secret_shapes():
    text = (
        "aws_key=AKIAABCDEFGHIJKLMNOP token=sk-live-abcdefghijklmnopqrstuvwx1234 "
        "postgres://user:hunter2@host:5432/db normal words stay untouched"
    )
    out = redact_secrets(text)
    assert "AKIAABCDEFGHIJKLMNOP" not in out
    assert "sk-live-abcdefghijklmnopqrstuvwx1234" not in out
    assert "hunter2" not in out
    assert "normal words stay untouched" in out


def test_write_degraded_marker_is_machine_readable_and_redacted(tmp_path):
    repo = tmp_path / "target-repo"
    repo.mkdir()
    path = write_degraded_marker(
        repo,
        unavailable=["findings-ledger", "liar-ledger", "job-inventory"],
        reasons=["PRAXIS_API_KEY unset; token=sk-live-abcdefghijklmnopqrstuvwx1234"],
    )
    assert repo not in path.parents
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["degraded"] is True
    assert set(data["unavailable"]) == {"findings-ledger", "liar-ledger", "job-inventory"}
    assert "sk-live-abcdefghijklmnopqrstuvwx1234" not in json.dumps(data)


def test_write_degraded_marker_requires_a_reason_named(tmp_path):
    repo = tmp_path / "target-repo"
    repo.mkdir()
    with pytest.raises(ValueError):
        write_degraded_marker(repo, unavailable=["job-inventory"], reasons=[])


def test_bootstrap_space_creates_it_when_absent(tmp_path, monkeypatch):
    repo = tmp_path / "target-repo"
    repo.mkdir()
    calls = []
    monkeypatch.setattr(_praxis, "_request",
                        lambda *a, **k: calls.append((a, k)) or {"spaceId": "x"})
    sid = bootstrap_space(repo)
    assert sid == space_name(repo)
    assert calls and calls[0][0] == ("POST", "/spaces")
    assert calls[0][1]["body"]["spaceId"] == sid


def test_bootstrap_space_is_idempotent_on_conflict(tmp_path, monkeypatch):
    repo = tmp_path / "target-repo"
    repo.mkdir()

    def _conflict(*a, **k):
        raise _praxis.PraxisUnreachable("Praxis POST /spaces -> HTTP 409: already exists")

    monkeypatch.setattr(_praxis, "_request", _conflict)
    sid = bootstrap_space(repo)  # must NOT raise — "assumes no existing space" is a no-op, not an error
    assert sid == space_name(repo)


def test_bootstrap_space_reraises_other_failures(tmp_path, monkeypatch):
    repo = tmp_path / "target-repo"
    repo.mkdir()

    def _boom(*a, **k):
        raise _praxis.PraxisUnreachable("Praxis POST /spaces -> HTTP 500: server error")

    monkeypatch.setattr(_praxis, "_request", _boom)
    with pytest.raises(_praxis.PraxisUnreachable):
        bootstrap_space(repo)


def test_backend_status_authenticated_when_key_set_and_whoami_ok(monkeypatch):
    monkeypatch.setenv("PRAXIS_API_KEY", "pxk_fake")
    monkeypatch.delenv("PRAXIS_AUTH_DISABLED", raising=False)
    ok_who = _praxis.WhoAmI("http://x", "org", "PRAXIS_ORG", "sub", "key", "org", True, "")
    monkeypatch.setattr(_praxis, "whoami", lambda: ok_who)
    status = backend_status()
    assert status == (True, True, False, ())


def test_backend_status_degraded_when_whoami_reports_mismatch(monkeypatch):
    monkeypatch.setenv("PRAXIS_API_KEY", "pxk_fake")
    monkeypatch.delenv("PRAXIS_AUTH_DISABLED", raising=False)
    bad_who = _praxis.WhoAmI("http://x", "org", "PRAXIS_ORG", "sub", "key", "other",
                             False, "key scoped to org 'other' but PRAXIS_ORG='org'")
    monkeypatch.setattr(_praxis, "whoami", lambda: bad_who)
    status = backend_status()
    assert status.degraded is True
    assert status.reachable is True
    assert status.authenticated is False
    assert status.reasons
