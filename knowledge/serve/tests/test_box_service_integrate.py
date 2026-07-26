"""Acceptance test for ticket R32 (two-level integration,
e1b7889e81114590bba894cbbe19b36d): given a finished job whose per-ticket
worktree merge and WORK-review panel have already happened in-session, the
box service performs exactly one further merge of the job branch into the
repo's main worktree — never a second, per-ticket merge — and a conflicting
merge (R34) leaves the job branch preserved and the main worktree exactly
where it was before the attempt.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from knowledge.serve.box_service_clone import RepoClone
from knowledge.serve.box_service_integrate import (
    IntegrationResult,
    MergeConflictError,
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


def _make_origin(tmp_path) -> str:
    """A throwaway upstream repo (R32's ``pr_base``'s real remote). Pushes
    into its checked-out branch are allowed so the conflict test can advance
    it directly, mirroring another job's already-merged-and-pushed work."""
    origin = str(tmp_path / "origin")
    subprocess.run(["git", "init", "-b", "main", origin], check=True, capture_output=True, text=True)
    _git("config", "user.email", "box@example.com", cwd=origin)
    _git("config", "user.name", "Box Service", cwd=origin)
    _write_commit("base.txt", origin, "base\n", "base commit")
    return origin


def _make_repo_clone_with_main_worktree(origin: str, tmp_path) -> RepoClone:
    """``git clone --bare`` wires the clone's own "origin" remote to
    ``origin`` automatically -- the main worktree (which shares the bare
    clone's config) already has everything it needs to fetch ``pr_base``."""
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
    _git("fetch", "origin", cwd=main_worktree_path)
    return RepoClone(origin_url=origin, clone_path=clone_path, main_worktree_path=main_worktree_path)


def _make_job_worktree_with_finished_branch(repo_clone: RepoClone, tmp_path, branch: str) -> str:
    """A job worktree already carrying the (in-session, af-build-owned)
    integrated per-ticket work as a single branch tip -- this module never
    re-does that merge, it only takes the branch whole. Created off the same
    bare clone as the main worktree, so the branch is immediately visible to
    it with no fetch or push required."""
    job_worktree = str(tmp_path / "box" / "repo" / "jobs" / "job-a")
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, job_worktree, "main"],
        cwd=repo_clone.clone_path,
        check=True,
        capture_output=True,
        text=True,
    )
    _write_commit("ticket-a.txt", job_worktree, "ticket-a\n", "ticket-a finished")
    _write_commit("ticket-b.txt", job_worktree, "ticket-b\n", "ticket-b finished")
    return job_worktree


def test_integration_performs_exactly_one_merge_and_no_per_ticket_merges(tmp_path):
    origin = _make_origin(tmp_path)
    repo_clone = _make_repo_clone_with_main_worktree(origin, tmp_path)
    _make_job_worktree_with_finished_branch(repo_clone, tmp_path, "job-a")

    merge_calls = []
    real_runner = subprocess.run

    def counting_runner(args, **kwargs):
        if len(args) >= 2 and args[0] == "git" and args[1] == "merge" and "--abort" not in args:
            merge_calls.append(args)
        return real_runner(args, **kwargs)

    lock = RepoIntegrationLock()
    result = integrate_job_branch(
        repo_clone,
        "job-a",
        "main",
        holder_id="box-1",
        lock=lock,
        runner=counting_runner,
    )

    assert isinstance(result, IntegrationResult)
    # Exactly one merge call — the box service's single further merge (R32),
    # never a second, per-ticket merge.
    assert len(merge_calls) == 1

    log = _git("log", "--oneline", cwd=repo_clone.main_worktree_path)
    assert "ticket-a finished" in log
    assert "ticket-b finished" in log

    # The lock is released after a completed integration, ready for the next job.
    assert lock.held_by(repo_clone.clone_path) is None


def test_conflicting_merge_preserves_job_branch_and_leaves_main_worktree_unchanged(tmp_path):
    origin = _make_origin(tmp_path)
    repo_clone = _make_repo_clone_with_main_worktree(origin, tmp_path)
    _make_job_worktree_with_finished_branch(repo_clone, tmp_path, "job-a")

    # A different, already-integrated job pushed a conflicting change to the
    # same path directly to the real upstream, after this job's branch point.
    _write_commit("ticket-a.txt", origin, "conflicting\n", "conflicting upstream commit")
    pre_merge_sha = _git("rev-parse", "HEAD", cwd=repo_clone.main_worktree_path).strip()

    lock = RepoIntegrationLock()
    try:
        integrate_job_branch(
            repo_clone, "job-a", "main", holder_id="box-1", lock=lock, runner=subprocess.run
        )
        raise AssertionError("expected MergeConflictError")
    except MergeConflictError as exc:
        assert "job-a" in str(exc)

    # The main worktree is reset to the new upstream tip (never partially
    # merged, R34) and the job branch is untouched.
    assert _git("rev-parse", "HEAD", cwd=repo_clone.main_worktree_path).strip() != pre_merge_sha
    status = _git("status", "--porcelain", cwd=repo_clone.main_worktree_path)
    assert status.strip() == ""
    assert _git("rev-parse", "job-a", cwd=repo_clone.clone_path).strip()

    # The lock is released even on failure, so a retry is never stranded.
    assert lock.held_by(repo_clone.clone_path) is None
