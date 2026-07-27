"""Acceptance test for ticket R62 (25d5a89b76cd41d3af82cafb64c7d625):

Given a crash between push and pull-request creation, replay opens exactly one pull request for
the existing branch rather than pushing again; given an unreconcilable stage, the job lands
needs-attention with the branch intact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from knowledge.serve.box_service_failures import FailureClass
from knowledge.serve.box_service_integrate import (
    IntegrationTarget,
    UnreconcilableDeliveryStageError,
    replay_integration_sequence,
    run_integration_sequence,
)
from knowledge.serve.box_service_integrate import RepoIntegrationLock
from knowledge.serve.box_service_models import DeliveryStage, Job, JobState


@dataclass
class Proc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class ScriptedRunner:
    calls: list = field(default_factory=list)
    head_sha: str = "cafef00d"

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
            return Proc(returncode=0)
        if sub == "merge" and "--abort" in args:
            return Proc()
        if sub == "rev-parse":
            return Proc(stdout=f"{self.head_sha}\n")
        if sub == "push":
            return Proc()
        raise AssertionError(f"unexpected git call: {args}")


def make_target(**overrides) -> IntegrationTarget:
    defaults = dict(
        main_worktree_path="/repos/widgets/main",
        origin_repo="git@github.com:acme/widgets.git",
        allowlisted_origin="git@github.com:acme/widgets.git",
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


def test_a_crash_after_push_records_publishing_before_pr_creation():
    # The FIRST (uninterrupted) run stamps the durable stage before each irreversible step.
    lock = RepoIntegrationLock()
    target = make_target()
    runner = ScriptedRunner()
    job = make_job()
    seen_stage_at_pr_creation = []

    def pr_creator(t, sha):
        seen_stage_at_pr_creation.append(job.delivery_stage)
        return "https://github.com/acme/widgets/pull/7"

    run_integration_sequence(
        target, holder_id="holder-1", lock=lock, runner=runner, pr_creator=pr_creator, job=job,
    )

    assert seen_stage_at_pr_creation == [DeliveryStage.OPENING_PR]
    assert job.delivery_stage == DeliveryStage.DELIVERED


def test_crash_between_push_and_pr_creation_replay_opens_exactly_one_pr_without_pushing_again():
    lock = RepoIntegrationLock()
    target = make_target()
    runner = ScriptedRunner()
    # Simulate the crash: the push already landed (stage recorded before the push, and the box
    # service died before pull-request creation confirmed).
    job = make_job(delivery_stage=DeliveryStage.PUBLISHING)

    pr_calls = []

    def pr_creator(t, sha):
        pr_calls.append((t, sha))
        return "https://github.com/acme/widgets/pull/7"

    result = replay_integration_sequence(
        target,
        holder_id="holder-1",
        lock=lock,
        job=job,
        runner=runner,
        pr_creator=pr_creator,
        existing_pr_lookup=lambda t: None,
        remote_ref_exists=True,
    )

    # Exactly one pull request opened.
    assert len(pr_calls) == 1
    assert result.pr_url == "https://github.com/acme/widgets/pull/7"
    assert job.delivery_stage == DeliveryStage.DELIVERED
    # Never pushed again.
    assert not any(c[0][1] == "push" for c in runner.calls)
    # Never re-reset / re-merged either — the branch already published is left as-is.
    assert not any(c[0][1] in ("reset", "merge") for c in runner.calls)


def test_replay_at_opening_pr_stage_with_no_pr_yet_opens_exactly_one():
    lock = RepoIntegrationLock()
    target = make_target()
    runner = ScriptedRunner()
    job = make_job(delivery_stage=DeliveryStage.OPENING_PR)
    pr_calls = []

    result = replay_integration_sequence(
        target,
        holder_id="holder-1",
        lock=lock,
        job=job,
        runner=runner,
        pr_creator=lambda t, sha: pr_calls.append(1) or "https://github.com/acme/widgets/pull/8",
        existing_pr_lookup=lambda t: None,
        remote_ref_exists=True,
    )

    assert len(pr_calls) == 1
    assert result.pr_url == "https://github.com/acme/widgets/pull/8"
    assert not any(c[0][1] == "push" for c in runner.calls)


def test_replay_reuses_an_already_open_pull_request_rather_than_opening_a_second():
    lock = RepoIntegrationLock()
    target = make_target()
    runner = ScriptedRunner()
    job = make_job(delivery_stage=DeliveryStage.OPENING_PR)
    pr_calls = []

    result = replay_integration_sequence(
        target,
        holder_id="holder-1",
        lock=lock,
        job=job,
        runner=runner,
        pr_creator=lambda t, sha: pr_calls.append(1) or "unused",
        existing_pr_lookup=lambda t: "https://github.com/acme/widgets/pull/9",
        remote_ref_exists=True,
    )

    assert pr_calls == []
    assert result.pr_url == "https://github.com/acme/widgets/pull/9"
    assert job.delivery_stage == DeliveryStage.DELIVERED


def test_unreconcilable_stage_lands_needs_attention_with_the_branch_intact():
    lock = RepoIntegrationLock()
    target = make_target()
    runner = ScriptedRunner()
    # The stage claims the pull request is being opened (so the branch must already be published)
    # but re-detection finds the remote ref missing — a contradiction replay must never guess past.
    job = make_job(delivery_stage=DeliveryStage.OPENING_PR)

    with pytest.raises(UnreconcilableDeliveryStageError):
        replay_integration_sequence(
            target,
            holder_id="holder-1",
            lock=lock,
            job=job,
            runner=runner,
            pr_creator=lambda t, sha: pytest.fail("must never open a PR when unreconcilable"),
            existing_pr_lookup=lambda t: None,
            remote_ref_exists=False,
        )

    assert job.state == JobState.NEEDS_ATTENTION
    assert job.failure_reason == FailureClass.DELIVERY_STAGE_UNRECONCILABLE.value
    # No git call at all — the branch is left completely untouched.
    assert runner.calls == []
    # The lock is released so a corrected retry is not stranded.
    assert lock.acquire(target.main_worktree_path, "holder-2") is True


def test_replay_with_nothing_published_yet_safely_runs_the_full_sequence():
    lock = RepoIntegrationLock()
    target = make_target()
    runner = ScriptedRunner()
    job = make_job(delivery_stage=DeliveryStage.PUBLISHING)
    pr_calls = []

    result = replay_integration_sequence(
        target,
        holder_id="holder-1",
        lock=lock,
        job=job,
        runner=runner,
        pr_creator=lambda t, sha: pr_calls.append(1) or "https://github.com/acme/widgets/pull/1",
        existing_pr_lookup=lambda t: None,
        remote_ref_exists=False,
    )

    assert len(pr_calls) == 1
    assert any(c[0][1] == "push" for c in runner.calls)
    assert result.merged_sha == "cafef00d"
    assert job.delivery_stage == DeliveryStage.DELIVERED
