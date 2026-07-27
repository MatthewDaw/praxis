"""Acceptance test for ticket R49 (d47383c821e844b7b5e13f36672cf659):

Given a group whose members all finished, exactly one commit and exactly one pull request are
produced, and the merges occurred in dispatch order in the main worktree — rather than one
merge/commit/PR per member.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from knowledge.serve.box_service_failures import FailureClass
from knowledge.serve.box_service_group_integrate import (
    GroupIntegrationTarget,
    run_group_integration_sequence,
)
from knowledge.serve.box_service_integrate import (
    MergeConflictError,
    PublishRefusedError,
    RepoIntegrationLock,
)
from knowledge.serve.box_service_models import Job, JobState


@dataclass
class Proc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class ScriptedRunner:
    #: branch name (as merged) that should conflict, or None if every merge succeeds.
    conflict_on: str | None = None
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
        if sub == "merge" and "--squash" in args:
            branch = args[-1]
            if branch == self.conflict_on:
                return Proc(returncode=1, stderr="CONFLICT")
            return Proc()
        if sub == "commit":
            return Proc()
        if sub == "rev-parse":
            return Proc(stdout="cafef00d\n")
        if sub == "push":
            return Proc()
        raise AssertionError(f"unexpected git call: {args}")


def make_target(**overrides) -> GroupIntegrationTarget:
    defaults = dict(
        main_worktree_path="/repos/widgets/main",
        origin_repo="git@github.com:acme/widgets.git",
        allowlisted_origin="git@github.com:acme/widgets.git",
        member_branches=["job/job-1", "job/job-2", "job/job-3"],
        pr_base="main",
        integration_ref="refs/heads/integrate/group-1",
    )
    defaults.update(overrides)
    return GroupIntegrationTarget(**defaults)


def make_job(job_id: str, branch: str) -> Job:
    return Job(
        id=job_id, project="af-build-remote-jobs", snapshot="prd-af-build-remote-jobs",
        state=JobState.COMPLETED, group_id="group-1",
    ), branch


def test_group_integration_produces_exactly_one_commit_and_one_pr_in_dispatch_order():
    lock = RepoIntegrationLock()
    target = make_target()
    runner = ScriptedRunner()
    pr_calls = []

    def pr_creator(t, sha):
        pr_calls.append((t, sha))
        return "https://github.com/acme/widgets/pull/9"

    result = run_group_integration_sequence(
        target, holder_id="holder-1", lock=lock, runner=runner, pr_creator=pr_creator,
    )

    # Merges happened, one per member, IN DISPATCH ORDER.
    squash_calls = [c for c in runner.calls if c[0][1] == "merge" and "--squash" in c[0]]
    assert [c[0][-1] for c in squash_calls] == target.member_branches

    # Exactly ONE commit — not one per member.
    commit_calls = [c for c in runner.calls if c[0][1] == "commit"]
    assert len(commit_calls) == 1

    # Exactly ONE push and ONE pull request — not one per member.
    push_calls = [c for c in runner.calls if c[0][1] == "push"]
    assert len(push_calls) == 1
    assert pr_calls == [(target, "cafef00d")]

    assert result.merged_sha == "cafef00d"
    assert result.pushed_ref == target.integration_ref
    assert result.pr_url == "https://github.com/acme/widgets/pull/9"

    # Everything ran against the main worktree only.
    assert all(cwd == target.main_worktree_path for _args, cwd in runner.calls)


def test_a_conflicting_member_preserves_every_branch_and_leaves_the_main_worktree_unmerged():
    lock = RepoIntegrationLock()
    target = make_target()
    runner = ScriptedRunner(conflict_on="job/job-2")
    job1, _ = make_job("job-1", "job/job-1")
    job2, _ = make_job("job-2", "job/job-2")
    job3, _ = make_job("job-3", "job/job-3")

    with pytest.raises(MergeConflictError):
        run_group_integration_sequence(
            target, holder_id="holder-1", lock=lock, runner=runner,
            pr_creator=lambda t, sha: "unused", jobs=[job1, job2, job3],
        )

    # Only job-2 (the conflicting member) is recorded as a failure.
    assert job2.state == JobState.NEEDS_ATTENTION
    assert job2.failure_reason == FailureClass.MERGE_CONFLICT.value
    assert job1.state == JobState.COMPLETED
    assert job3.state == JobState.COMPLETED

    # The third member's branch was never even attempted (dispatch-order short-circuit).
    squash_calls = [c for c in runner.calls if c[0][1] == "merge" and "--squash" in c[0]]
    assert [c[0][-1] for c in squash_calls] == ["job/job-1", "job/job-2"]

    # The conflict was cleaned up by resetting back to the resolved PR base, never committed.
    assert not any(c[0][1] == "commit" for c in runner.calls)
    assert not any(c[0][1] == "push" for c in runner.calls)


def test_a_refused_publish_never_reaches_pull_request_creation():
    lock = RepoIntegrationLock()
    target = make_target(integration_ref="refs/heads/not-reserved/group-1")
    runner = ScriptedRunner()
    pr_calls = []

    with pytest.raises(PublishRefusedError):
        run_group_integration_sequence(
            target, holder_id="holder-1", lock=lock, runner=runner,
            pr_creator=lambda t, sha: pr_calls.append(1) or "unused",
        )

    assert pr_calls == []
    assert not any(c[0][1] == "push" for c in runner.calls)
    # The lock is still released so a corrected retry is not stranded.
    assert lock.acquire(target.main_worktree_path, "holder-2") is True


def test_a_single_member_group_still_produces_one_commit_and_one_pr():
    lock = RepoIntegrationLock()
    target = make_target(member_branches=["job/job-1"])
    runner = ScriptedRunner()

    result = run_group_integration_sequence(
        target, holder_id="holder-1", lock=lock, runner=runner,
        pr_creator=lambda t, sha: "https://github.com/acme/widgets/pull/1",
    )

    commit_calls = [c for c in runner.calls if c[0][1] == "commit"]
    assert len(commit_calls) == 1
    assert result.pr_url == "https://github.com/acme/widgets/pull/1"
