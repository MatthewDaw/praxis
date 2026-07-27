"""Acceptance test for ticket R58 (3161796401b44e21beb58a8620edca85):

Given a job branch merging into the main worktree, none of a repo-supplied hook, a
``.gitattributes`` merge driver, or a clean/smudge filter ever execute during integration — the
merge either completes or fails on its content alone. Unlike the other ``box_service_integrate``
tests (which use a scripted fake runner), this exercises REAL ``git`` subprocesses against a real
repo on disk: hooks, merge drivers, and filters are actual OS-level side effects a fake runner
can't observe.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from knowledge.serve.box_service_integrate import (
    IntegrationTarget,
    MergeConflictError,
    RepoIntegrationLock,
    run_integration_sequence,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc


def _write(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _init_repo(main: Path, bare: Path) -> None:
    _git(main.parent, "init", "--bare", str(bare))
    _git(main.parent, "init", "-b", "main", str(main))
    _git(main, "config", "user.email", "box@example.com")
    _git(main, "config", "user.name", "box")
    _git(main, "remote", "add", "origin", str(bare))


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    main = tmp_path / "main"
    bare = tmp_path / "origin.git"
    _init_repo(main, bare)

    # Legitimate, pre-existing config a real box repo might carry for its own purposes: a
    # tracked hooks directory, a custom merge driver, and a clean/smudge filter — all of which
    # a session-authored job branch must never be able to trigger merely by shaping content that
    # names them.
    _git(main, "config", "core.hooksPath", ".githooks")
    _git(main, "config", "merge.marker.driver", "sh -c \"touch MARKER_DRIVER && cp \\\"$2\\\" \\\"$2\\\"\" -- %O %A %B")
    _git(main, "config", "filter.marker.clean", "cat")
    _git(main, "config", "filter.marker.smudge", "sh -c 'touch MARKER_SMUDGE; cat'")

    _write(main / ".gitattributes", "conflict.txt merge=marker\nsecret.txt filter=marker\n")
    _write(
        main / ".githooks" / "post-merge",
        "#!/bin/sh\ntouch MARKER_HOOK\n",
        executable=True,
    )
    _write(main / "base.txt", "base\n")
    _write(main / "conflict.txt", "base\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "base")
    _git(main, "push", "origin", "main")
    return main


def _make_lock() -> RepoIntegrationLock:
    return RepoIntegrationLock()


def test_conflicting_merge_driver_never_executes(repo: Path) -> None:
    """A job branch whose content conflicts on a path bound to a custom merge driver never gets
    that driver invoked: the merge fails on content alone (a real, unresolved conflict), not via
    the driver silently resolving it."""
    _git(repo, "checkout", "-b", "job/1")
    _write(repo / "conflict.txt", "job change\n")
    _git(repo, "commit", "-am", "job change")

    _git(repo, "checkout", "main")
    _write(repo / "conflict.txt", "main change\n")
    _git(repo, "commit", "-am", "main change")
    _git(repo, "push", "origin", "main")

    target = IntegrationTarget(
        main_worktree_path=str(repo),
        origin_repo=str(repo.parent / "origin.git"),
        allowlisted_origin=str(repo.parent / "origin.git"),
        job_branch="job/1",
        pr_base="main",
        integration_ref="refs/heads/integrate/job-1",
    )

    with pytest.raises(MergeConflictError):
        run_integration_sequence(
            target,
            holder_id="holder-1",
            lock=_make_lock(),
            runner=subprocess.run,
            pr_creator=lambda t, sha: "https://example.invalid/pr/1",
        )

    assert not (repo / "MARKER_DRIVER").exists(), (
        "the .gitattributes merge driver executed during integration"
    )


def test_hook_and_smudge_filter_never_execute_on_a_clean_merge(repo: Path) -> None:
    """A job branch that adds a new file bound to a clean/smudge filter, and that merges cleanly
    (no content conflict), never triggers that filter, and the post-merge hook a tracked
    ``core.hooksPath`` points at never runs either."""
    _git(repo, "checkout", "-b", "job/2")
    _write(repo / "secret.txt", "top secret\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add secret.txt")
    _git(repo, "checkout", "main")

    target = IntegrationTarget(
        main_worktree_path=str(repo),
        origin_repo=str(repo.parent / "origin.git"),
        allowlisted_origin=str(repo.parent / "origin.git"),
        job_branch="job/2",
        pr_base="main",
        integration_ref="refs/heads/integrate/job-2",
    )

    run_integration_sequence(
        target,
        holder_id="holder-2",
        lock=_make_lock(),
        runner=subprocess.run,
        pr_creator=lambda t, sha: "https://example.invalid/pr/2",
    )

    assert not (repo / "MARKER_HOOK").exists(), "the post-merge hook executed during integration"
    assert not (repo / "MARKER_SMUDGE").exists(), "the smudge filter executed during integration"
    assert (repo / "secret.txt").read_text() == "top secret\n"
