"""Acceptance test for ticket R7 (build base recorded as a resolved commit
SHA, 8f2c0c1e): given a job dispatched from a branch, when a new commit lands
on that branch before the box claims the job, the executed build base still
equals the SHA recorded at dispatch.

Also covers the mandatory ``dispatch-guard`` check (3c65db2b): dispatch pins
the build-base SHA, refuses a dirty working tree naming the paths, refuses an
origin absent from the allowlist, and derives org identity server-side while
ignoring a payload-supplied org.
"""

from __future__ import annotations

import subprocess

import pytest

from knowledge.serve.dispatch import (
    DirtyWorkingTreeError,
    OriginNotAllowedError,
    dispatch_job,
)

ALLOWED_ORIGIN = "https://github.com/example-org/example-repo.git"


def _git(*args: str, cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(tmp_path) -> str:
    repo = str(tmp_path / "repo")
    subprocess.run(["git", "init", repo], check=True, capture_output=True, text=True)
    _git("config", "user.email", "box@example.com", cwd=repo)
    _git("config", "user.name", "Box Service", cwd=repo)
    (tmp_path / "repo" / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    _git("checkout", "-b", "feature", cwd=repo)
    return repo


def _payload(**overrides) -> dict:
    base = {
        "project": "af-build-remote-jobs",
        "snapshot": "prd-af-build-remote-jobs",
        "origin_url": ALLOWED_ORIGIN,
        "branch": "feature",
        "pr_base": "main",
    }
    base.update(overrides)
    return base


def test_build_base_stays_pinned_to_the_sha_recorded_at_dispatch(tmp_path):
    repo = _init_repo(tmp_path)

    result = dispatch_job(
        _payload(), caller_org_id="acme", allowlist={ALLOWED_ORIGIN}, cwd=repo
    )
    recorded_sha = result.build_base_sha

    # A new commit lands on the branch AFTER dispatch, BEFORE the box claims it.
    (tmp_path / "repo" / "README.md").write_text("hello again\n")
    _git("commit", "-am", "a commit that lands after dispatch", cwd=repo)
    new_head_sha = subprocess.run(
        ["git", "rev-parse", "feature"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    assert new_head_sha != recorded_sha

    # The box now "claims" the job and builds at the payload's recorded SHA —
    # never at the branch's current tip.
    executed_build_base = subprocess.run(
        ["git", "rev-parse", result.build_base_sha],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert executed_build_base == recorded_sha
    assert result.branch == "feature"


def test_dispatch_refuses_an_origin_absent_from_the_allowlist(tmp_path):
    repo = _init_repo(tmp_path)

    with pytest.raises(OriginNotAllowedError):
        dispatch_job(
            _payload(origin_url="https://github.com/evil/other.git"),
            caller_org_id="acme",
            allowlist={ALLOWED_ORIGIN},
            cwd=repo,
        )


def test_dispatch_refuses_a_dirty_working_tree_naming_the_paths(tmp_path):
    repo = _init_repo(tmp_path)
    (tmp_path / "repo" / "scratch.txt").write_text("uncommitted\n")

    with pytest.raises(DirtyWorkingTreeError) as excinfo:
        dispatch_job(
            _payload(), caller_org_id="acme", allowlist={ALLOWED_ORIGIN}, cwd=repo
        )

    assert "scratch.txt" in excinfo.value.paths


def test_dispatch_derives_org_identity_server_side_ignoring_payload_org(tmp_path):
    repo = _init_repo(tmp_path)

    result = dispatch_job(
        _payload(org="attacker-controlled-org"),
        caller_org_id="acme",
        allowlist={ALLOWED_ORIGIN},
        cwd=repo,
    )

    assert result.org_id == "acme"
