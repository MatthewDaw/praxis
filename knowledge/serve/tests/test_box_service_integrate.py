"""Acceptance test for ticket R33 (9b8c0e7d77144fae99ab5dc215418221):

Given a finished job, the reset-to-PR-base, merge, publish (remote-ref update), and pull-request
creation all execute from the main worktree and never a job worktree, under a per-repo integration
lock held for the whole sequence; given two same-repo jobs finishing simultaneously their
sequences do not overlap (covered in test_integration_lock.py); given a dirty main worktree or one
holding an unpushed commit, integration refuses, records needs-attention, and leaves its contents
untouched (covered in test_integration_lock.py); a conflicting merge preserves the job branch and
leaves the main worktree unmerged (R34); and a refused remote-ref update never opens a pull
request.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from knowledge.serve.box_service_failures import FailureClass
from knowledge.serve.box_service_integrate import (
    IntegrationTarget,
    MergeConflictError,
    PublishRefusedError,
    RepoIntegrationLock,
    run_integration_sequence,
)
from knowledge.serve.box_service_models import Job, JobState


@dataclass
class Proc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class ScriptedRunner:
    merge_ok: bool = True
    calls: list = field(default_factory=list)

    def __call__(self, args, cwd, capture_output=True, text=True, check=False):
        self.calls.append((tuple(args), cwd))
        sub = args[1] if len(args) > 1 else None
        if sub == "config":
            return Proc()
        if sub == "status":
            return Proc(stdout="")
        if sub == "fetch":
            return Proc()
        if sub == "log":
            return Proc(stdout="")
        if sub == "reset":
            return Proc()
        if sub == "merge" and "--abort" not in args:
            return Proc(returncode=0 if self.merge_ok else 1, stderr="" if self.merge_ok else "CONFLICT")
        if sub == "merge" and "--abort" in args:
            return Proc()
        if sub == "rev-parse":
            return Proc(stdout="cafef00d\n")
        if sub == "push":
            return Proc()
        raise AssertionError(f"unexpected git call: {args}")


def make_target(**overrides) -> IntegrationTarget:
    defaults = dict(
        main_worktree_path="/repos/widgets/main",
        origin_repo="git@github.com:acme/widgets.git",
        job_branch="job/job-1",
        pr_base="main",
        integration_ref="refs/heads/integrate/job-1",
    )
    defaults.update(overrides)
    return IntegrationTarget(**defaults)


def make_job(**overrides) -> Job:
    defaults = dict(
        id="job-1", project="af-build-remote-jobs", snapshot="prd-af-build-remote-jobs",
        state=JobState.RUNNING,
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_the_whole_sequence_runs_only_against_the_main_worktree_path():
    lock = RepoIntegrationLock()
    target = make_target()
    runner = ScriptedRunner()
    pr_calls = []

    def pr_creator(t, sha):
        pr_calls.append((t, sha))
        return "https://github.com/acme/widgets/pull/7"

    result = run_integration_sequence(
        target, holder_id="holder-1", lock=lock, runner=runner, pr_creator=pr_creator
    )

    assert all(cwd == target.main_worktree_path for _args, cwd in runner.calls)
    assert result.merged_sha == "cafef00d"
    assert result.pushed_ref == target.integration_ref
    assert result.pr_url == "https://github.com/acme/widgets/pull/7"
    assert pr_calls == [(target, "cafef00d")]
    # Exactly one merge call — no repeated / per-ticket merge.
    merge_calls = [c for c in runner.calls if c[0][1] == "merge" and "--abort" not in c[0]]
    assert len(merge_calls) == 1


def test_merge_conflict_preserves_the_job_branch_and_records_needs_attention():
    lock = RepoIntegrationLock()
    target = make_target()
    runner = ScriptedRunner(merge_ok=False)
    job = make_job()

    with pytest.raises(MergeConflictError):
        run_integration_sequence(
            target, holder_id="holder-1", lock=lock, runner=runner,
            pr_creator=lambda t, sha: "unused", job=job,
        )

    assert job.state == JobState.NEEDS_ATTENTION
    assert job.failure_reason == FailureClass.MERGE_CONFLICT.value
    # The merge was aborted, never left in progress.
    assert any(c[0] == ("git", "merge", "--abort") for c in runner.calls)
    # No publish was attempted after a conflict.
    assert not any(c[0][1] == "push" for c in runner.calls)


def test_a_refused_publish_never_reaches_pull_request_creation():
    lock = RepoIntegrationLock()
    target = make_target(integration_ref="refs/heads/not-reserved/job-1")
    runner = ScriptedRunner()
    pr_calls = []

    with pytest.raises(PublishRefusedError):
        run_integration_sequence(
            target, holder_id="holder-1", lock=lock, runner=runner,
            pr_creator=lambda t, sha: pr_calls.append(1) or "unused",
        )

    assert pr_calls == []
    assert not any(c[0][1] == "push" for c in runner.calls)
    # The lock is still released so a corrected retry is not stranded.
    assert lock.acquire(target.main_worktree_path, "holder-2") is True
