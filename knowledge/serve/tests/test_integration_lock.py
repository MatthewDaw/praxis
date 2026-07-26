"""Covers the ``integration-serialized-per-repo`` build-validation check
(bd893f6d83b74f7f90b4ea00f4d88ebc): two same-repo jobs finishing
simultaneously do not overlap their reset-merge sequences, and integration
refuses rather than resetting a main worktree that is dirty or holds an
unpushed commit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from knowledge.serve.box_service_clone import RepoClone
from knowledge.serve.box_service_integrate import (
    IntegrationLockedError,
    MainWorktreeDirtyError,
    RepoIntegrationLock,
    integrate_job_branch,
)


def _git(*args: str, cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout


def _write_commit(path: str, cwd: str, content: str, message: str) -> str:
    Path(cwd, path).write_text(content)
    _git("add", path, cwd=cwd)
    _git("commit", "-m", message, cwd=cwd)
    return _git("rev-parse", "HEAD", cwd=cwd).strip()


def _make_repo_clone_with_main_worktree(tmp_path) -> RepoClone:
    origin = str(tmp_path / "origin")
    subprocess.run(["git", "init", "-b", "main", origin], check=True, capture_output=True, text=True)
    _git("config", "user.email", "box@example.com", cwd=origin)
    _git("config", "user.name", "Box Service", cwd=origin)
    _write_commit("base.txt", origin, "base\n", "base commit")

    clone_path = str(tmp_path / "box" / "repo.git")
    subprocess.run(
        ["git", "clone", "--bare", origin, clone_path], check=True, capture_output=True, text=True
    )
    main_worktree_path = str(tmp_path / "box" / "repo" / "main")
    subprocess.run(
        ["git", "worktree", "add", main_worktree_path, "main"],
        cwd=clone_path,
        check=True,
        capture_output=True,
        text=True,
    )
    # ``git clone --bare`` already wires the clone's own "origin" remote to
    # the true upstream -- the main worktree, sharing the bare clone's
    # config, has everything it needs to fetch ``pr_base`` without further
    # remote setup.
    return RepoClone(origin_url=origin, clone_path=clone_path, main_worktree_path=main_worktree_path)


def test_second_holder_is_refused_while_first_holds_the_repo_lock():
    lock = RepoIntegrationLock()
    assert lock.acquire("repo-1", "box-a") is True
    # A different, live holder is refused -- reset/merge sequences for the
    # same repo never interleave.
    assert lock.acquire("repo-1", "box-b") is False
    assert lock.held_by("repo-1") == "box-a"

    assert lock.release("repo-1", "box-a") is True
    # Now free: a different holder may proceed.
    assert lock.acquire("repo-1", "box-b") is True


def test_stale_lock_is_reclaimable_by_a_new_holder_so_a_dead_holder_never_stalls_it():
    now = [0.0]
    lock = RepoIntegrationLock(clock=lambda: now[0], ttl=10.0)

    assert lock.acquire("repo-1", "box-a") is True
    now[0] = 5.0
    assert lock.acquire("repo-1", "box-b") is False  # still live

    now[0] = 20.0  # past the ttl with no heartbeat
    assert lock.acquire("repo-1", "box-b") is True
    assert lock.held_by("repo-1") == "box-b"


def test_two_same_repo_integrations_do_not_overlap_their_reset_merge_sequences(tmp_path):
    repo_clone = _make_repo_clone_with_main_worktree(tmp_path)
    lock = RepoIntegrationLock()

    # box-1's integration is in flight (lock held, not yet released).
    assert lock.acquire(repo_clone.clone_path, "box-1") is True

    try:
        integrate_job_branch(
            repo_clone,
            "does-not-matter",
            "main",
            holder_id="box-2",
            lock=lock,
            runner=subprocess.run,
        )
        raise AssertionError("expected IntegrationLockedError")
    except IntegrationLockedError:
        pass

    # box-2's attempt never touched the main worktree.
    status = _git("status", "--porcelain", cwd=repo_clone.main_worktree_path)
    assert status.strip() == ""


def test_integration_refuses_a_dirty_main_worktree_rather_than_resetting_it(tmp_path):
    repo_clone = _make_repo_clone_with_main_worktree(tmp_path)
    Path(repo_clone.main_worktree_path, "uncommitted.txt").write_text("not committed\n")
    lock = RepoIntegrationLock()

    try:
        integrate_job_branch(
            repo_clone, "does-not-matter", "main", holder_id="box-1", lock=lock, runner=subprocess.run
        )
        raise AssertionError("expected MainWorktreeDirtyError")
    except MainWorktreeDirtyError:
        pass

    # The dirty file survives untouched -- the reset never ran.
    assert Path(repo_clone.main_worktree_path, "uncommitted.txt").read_text() == "not committed\n"
    assert lock.held_by(repo_clone.clone_path) is None


def test_integration_refuses_a_main_worktree_holding_an_unpushed_commit(tmp_path):
    repo_clone = _make_repo_clone_with_main_worktree(tmp_path)
    _write_commit(
        "local-only.txt", repo_clone.main_worktree_path, "local\n", "unpushed local commit"
    )
    lock = RepoIntegrationLock()

    try:
        integrate_job_branch(
            repo_clone, "does-not-matter", "main", holder_id="box-1", lock=lock, runner=subprocess.run
        )
        raise AssertionError("expected MainWorktreeDirtyError")
    except MainWorktreeDirtyError:
        pass

    # The unpushed commit is still there -- the reset never ran.
    assert (
        _git("log", "-1", "--format=%s", cwd=repo_clone.main_worktree_path).strip()
        == "unpushed local commit"
    )
    assert lock.held_by(repo_clone.clone_path) is None
