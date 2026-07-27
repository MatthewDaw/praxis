"""Acceptance test for ticket R40 (4ca692a879f049edb12fda9cb1dc4efe): a job
worktree is deleted once its work has merged into the main worktree and its
tail has persisted, while the repo's clone and main worktree persist across
jobs, and session reaping never deletes a worktree nor does deletion ever
precede integration.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from knowledge.serve.box_service_activity_tail import ActivityTailStore
from knowledge.serve.box_service_clone import RepoClone
from knowledge.serve.box_service_job_worktree import JobWorktreeManager
from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.box_service_worktree_cleanup import (
    WorktreeCleanupNotReadyError,
    cleanup_job_worktree,
)
from knowledge.serve.session_launcher import SessionLauncher


def _git(*args: str, cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout


def _origin_with_one_commit(tmp_path) -> tuple[str, str]:
    origin = str(tmp_path / "origin")
    subprocess.run(["git", "init", "-b", "main", origin], check=True, capture_output=True, text=True)
    _git("config", "user.email", "box@example.com", cwd=origin)
    _git("config", "user.name", "Box Service", cwd=origin)
    (tmp_path / "origin" / "file.txt").write_text("v1\n")
    _git("add", "file.txt", cwd=origin)
    _git("commit", "-m", "first", cwd=origin)
    sha = _git("rev-parse", "HEAD", cwd=origin).strip()
    return origin, sha


def _bare_clone_with_main_worktree(origin: str, tmp_path) -> RepoClone:
    clone_path = str(tmp_path / "box" / "repo.git")
    main_worktree_path = str(tmp_path / "box" / "repo" / "main")
    subprocess.run(
        ["git", "clone", "--bare", origin, clone_path], check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "worktree", "add", main_worktree_path],
        cwd=clone_path, check=True, capture_output=True, text=True,
    )
    return RepoClone(origin_url=origin, clone_path=clone_path, main_worktree_path=main_worktree_path)


def _make_job_with_worktree(tmp_path) -> tuple[Job, RepoClone, str]:
    origin, sha = _origin_with_one_commit(tmp_path)
    repo_clone = _bare_clone_with_main_worktree(origin, tmp_path)
    manager = JobWorktreeManager()
    job_worktree = manager.ensure(repo_clone, "job-1", sha)
    job = Job(id="job-1", project="p", snapshot="s", state=JobState.RUNNING,
              worktree_path=job_worktree.path)
    return job, repo_clone, sha


def test_merged_and_tail_persisted_deletes_job_worktree_but_clone_and_main_worktree_remain(tmp_path):
    job, repo_clone, _sha = _make_job_with_worktree(tmp_path)
    assert Path(job.worktree_path).is_dir()

    store = ActivityTailStore()
    store.append(job, "final output\n")

    removed = cleanup_job_worktree(
        job, merged=True, tail_store=store, clone_path=repo_clone.clone_path,
    )

    assert removed is True
    assert job.worktree_path is None
    # The job's own worktree is gone...
    assert not Path(repo_clone.clone_path).parent.joinpath("jobs", "job-1").exists()
    # ...while the repo's bare clone and its main worktree remain untouched.
    assert Path(repo_clone.clone_path).exists()
    assert Path(repo_clone.main_worktree_path).is_dir()
    assert (Path(repo_clone.main_worktree_path) / "file.txt").exists()


def test_integration_not_run_leaves_worktree_in_place_even_after_session_reaping(tmp_path):
    job, repo_clone, _sha = _make_job_with_worktree(tmp_path)
    worktree_path = job.worktree_path
    job.session_id = "sess-1"

    store = ActivityTailStore()

    # Deletion must never precede integration: attempting cleanup before the
    # job has merged is refused, not silently skipped or downgraded.
    with pytest.raises(WorktreeCleanupNotReadyError):
        cleanup_job_worktree(job, merged=False, tail_store=store, clone_path=repo_clone.clone_path)
    assert Path(worktree_path).is_dir()
    assert job.worktree_path == worktree_path

    # Session reaping never deletes a worktree — it only reaps the agent
    # session, and has no effect on the worktree at all.
    launcher = SessionLauncher(
        runner=lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr="")
    )
    assert launcher.terminate(job.session_id) is True
    assert Path(worktree_path).is_dir()
    assert job.worktree_path == worktree_path


def test_cleanup_refuses_when_tail_has_not_persisted(tmp_path):
    job, repo_clone, _sha = _make_job_with_worktree(tmp_path)
    worktree_path = job.worktree_path
    store = ActivityTailStore()  # nothing ever appended for this job

    with pytest.raises(WorktreeCleanupNotReadyError):
        cleanup_job_worktree(job, merged=True, tail_store=store, clone_path=repo_clone.clone_path)

    assert Path(worktree_path).is_dir()
    assert job.worktree_path == worktree_path
