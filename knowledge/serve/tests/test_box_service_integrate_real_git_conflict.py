"""Acceptance test for ticket R34 (8d5b63f26aa24beba863c701492c253b):

Given a job branch that conflicts with the intended PR base, integration exits non-zero (raises),
the job branch still exists with its commits, and the main worktree has no conflict markers and no
partial merge in progress.

Unlike ``test_box_service_integrate.py``'s ``test_merge_conflict_preserves_the_job_branch_and_records_
needs_attention`` (a ``ScriptedRunner`` fake), this drives ``run_integration_sequence`` against REAL
git repositories so a genuine merge conflict is produced by real git and the physical, on-disk
properties the acceptance condition cares about — the job branch ref/commits still resolving, no
``<<<<<<<`` conflict markers left in the working tree, and no ``MERGE_HEAD`` (partial-merge state) —
are asserted against the real filesystem, not a fake's bookkeeping.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from knowledge.serve.box_service_failures import FailureClass
from knowledge.serve.box_service_integrate import (
    IntegrationTarget,
    MergeConflictError,
    RepoIntegrationLock,
    run_integration_sequence,
)
from knowledge.serve.box_service_models import Job, JobState


def _git(*args: str, cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout


def _init_repo_with_conflicting_history(tmp_path) -> tuple[str, str, str]:
    """An ``origin`` repo whose ``main`` advances past a job branch's fork point with a conflicting
    edit to the same line, plus a ``main_worktree`` clone with the job branch already present
    locally (as if built by an earlier job-worktree stage). Returns
    (origin_path, main_worktree_path, job_commit_sha).
    """
    origin = str(tmp_path / "origin")
    subprocess.run(["git", "init", "-b", "main", origin], check=True, capture_output=True, text=True)
    _git("config", "user.email", "box@example.com", cwd=origin)
    _git("config", "user.name", "Box Service", cwd=origin)
    (Path(origin) / "file.txt").write_text("base\n")
    _git("add", "file.txt", cwd=origin)
    _git("commit", "-m", "base", cwd=origin)

    main_worktree = str(tmp_path / "main_worktree")
    subprocess.run(["git", "clone", origin, main_worktree], check=True, capture_output=True, text=True)
    _git("config", "user.email", "box@example.com", cwd=main_worktree)
    _git("config", "user.name", "Box Service", cwd=main_worktree)

    # The job branch, forked from base, edits the shared line one way.
    _git("checkout", "-b", "job/conflict", cwd=main_worktree)
    (Path(main_worktree) / "file.txt").write_text("job-side-change\n")
    _git("add", "file.txt", cwd=main_worktree)
    _git("commit", "-m", "job change", cwd=main_worktree)
    job_sha = _git("rev-parse", "HEAD", cwd=main_worktree).strip()
    _git("checkout", "main", cwd=main_worktree)

    # origin's main independently advances with a conflicting edit to the same line.
    (Path(origin) / "file.txt").write_text("origin-side-change\n")
    _git("add", "file.txt", cwd=origin)
    _git("commit", "-m", "origin advances", cwd=origin)

    return origin, main_worktree, job_sha


def make_target(main_worktree: str, origin: str) -> IntegrationTarget:
    return IntegrationTarget(
        main_worktree_path=main_worktree,
        origin_repo=origin,
        job_branch="job/conflict",
        pr_base="main",
        integration_ref="refs/heads/integrate/job-conflict",
    )


def test_real_merge_conflict_leaves_job_branch_intact_and_worktree_clean(tmp_path):
    origin, main_worktree, job_sha = _init_repo_with_conflicting_history(tmp_path)
    target = make_target(main_worktree, origin)
    lock = RepoIntegrationLock()
    job = Job(id="job-1", project="af-build-remote-jobs", snapshot="prd-af-build-remote-jobs",
               state=JobState.RUNNING)

    with pytest.raises(MergeConflictError):
        run_integration_sequence(
            target,
            holder_id="holder-1",
            lock=lock,
            pr_creator=lambda t, sha: pytest.fail("no PR should be created on a conflict"),
            job=job,
        )

    # The job records a needs-attention terminal state for a distinct, machine-readable reason.
    assert job.state == JobState.NEEDS_ATTENTION
    assert job.failure_reason == FailureClass.MERGE_CONFLICT.value

    # The job branch still exists with its commits — untouched by the aborted merge.
    assert _git("rev-parse", "job/conflict", cwd=main_worktree).strip() == job_sha
    assert "job change" in _git("log", "-1", "--format=%s", "job/conflict", cwd=main_worktree)

    # The main worktree holds no partial-merge state...
    assert not (Path(main_worktree) / ".git" / "MERGE_HEAD").exists()
    status = _git("status", "--porcelain", cwd=main_worktree)
    assert status.strip() == ""

    # ...and no conflict markers were left in any tracked file.
    content = (Path(main_worktree) / "file.txt").read_text()
    assert "<<<<<<<" not in content
    assert "=======" not in content
    assert ">>>>>>>" not in content
    # It reflects the reset PR-base state (origin's advanced main), not a half-applied merge.
    assert content == "origin-side-change\n"
