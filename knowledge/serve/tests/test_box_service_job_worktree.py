"""Acceptance test for ticket R11 (job worktree per build-base SHA,
0c9311da5ff44a19bc2d755033154d57): given two concurrent jobs on one repo,
each has a distinct worktree path checked out at its own build-base SHA, and
neither observes the other's uncommitted files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from knowledge.serve.box_service_clone import RepoClone
from knowledge.serve.box_service_job_worktree import (
    JobWorktreeError,
    JobWorktreeManager,
)


def _git(*args: str, cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout


def _init_origin_with_two_commits(tmp_path) -> tuple[str, str, str]:
    """A throwaway repo with two commits on ``main``; returns (path, sha1, sha2)."""
    origin = str(tmp_path / "origin")
    subprocess.run(["git", "init", "-b", "main", origin], check=True, capture_output=True, text=True)
    _git("config", "user.email", "box@example.com", cwd=origin)
    _git("config", "user.name", "Box Service", cwd=origin)
    (tmp_path / "origin" / "file.txt").write_text("v1\n")
    _git("add", "file.txt", cwd=origin)
    _git("commit", "-m", "first", cwd=origin)
    sha1 = _git("rev-parse", "HEAD", cwd=origin).strip()
    (tmp_path / "origin" / "file.txt").write_text("v2\n")
    _git("add", "file.txt", cwd=origin)
    _git("commit", "-m", "second", cwd=origin)
    sha2 = _git("rev-parse", "HEAD", cwd=origin).strip()
    return origin, sha1, sha2


def _bare_clone(origin: str, tmp_path) -> RepoClone:
    clone_path = str(tmp_path / "box" / "repo.git")
    subprocess.run(
        ["git", "clone", "--bare", origin, clone_path], check=True, capture_output=True, text=True
    )
    return RepoClone(
        origin_url=origin,
        clone_path=clone_path,
        main_worktree_path=str(tmp_path / "box" / "repo" / "main"),
    )


def test_two_concurrent_jobs_get_distinct_worktrees_at_their_own_sha_and_do_not_leak_uncommitted_files(
    tmp_path,
):
    origin, sha1, sha2 = _init_origin_with_two_commits(tmp_path)
    repo_clone = _bare_clone(origin, tmp_path)
    manager = JobWorktreeManager()

    job_a = manager.ensure(repo_clone, "job-a", sha1)
    job_b = manager.ensure(repo_clone, "job-b", sha2)

    # Distinct worktree paths for two concurrent jobs on the same repo.
    assert job_a.path != job_b.path

    # Each worktree is checked out at its own recorded build-base SHA.
    assert _git("rev-parse", "HEAD", cwd=job_a.path).strip() == sha1
    assert _git("rev-parse", "HEAD", cwd=job_b.path).strip() == sha2

    # An uncommitted file dropped in job A's worktree is invisible from job B's.
    Path(job_a.path, "scratch.txt").write_text("job-a-only\n")
    assert not (Path(job_b.path) / "scratch.txt").exists()
    assert (Path(job_a.path) / "scratch.txt").exists()


def test_same_job_id_resumed_reuses_its_existing_worktree_without_a_second_add(tmp_path):
    origin, sha1, _sha2 = _init_origin_with_two_commits(tmp_path)
    repo_clone = _bare_clone(origin, tmp_path)
    manager = JobWorktreeManager()

    first = manager.ensure(repo_clone, "job-a", sha1)
    second = manager.ensure(repo_clone, "job-a", sha1)

    assert first.path == second.path


def test_worktree_add_failure_raises_job_worktree_error(tmp_path):
    origin, sha1, _sha2 = _init_origin_with_two_commits(tmp_path)
    repo_clone = _bare_clone(origin, tmp_path)

    def failing_runner(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="fatal: bad object")

    manager = JobWorktreeManager(runner=failing_runner, path_exists=lambda p: False)

    try:
        manager.ensure(repo_clone, "job-a", "not-a-real-sha")
        raise AssertionError("expected JobWorktreeError")
    except JobWorktreeError as exc:
        assert "job-a" in str(exc)
