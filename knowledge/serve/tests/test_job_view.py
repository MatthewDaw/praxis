"""Acceptance test for ticket R80 (588e65ae41da434dbab86b1a47c70e20):

Given a completed job, its row carries the branch and PR URL and both are visible in the job
view; given a failed job, its row carries the failure reason and the failing command's output and
both are visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from knowledge.serve.box_service_failures import FailureClass, record_failure
from knowledge.serve.box_service_integrate import (
    IntegrationTarget,
    MergeConflictError,
    RepoIntegrationLock,
    run_integration_sequence,
)
from knowledge.serve.box_service_models import Job, JobState, job_view, mark_completed


def make_job(**overrides) -> Job:
    defaults = dict(
        id="job-1", project="af-build-remote-jobs", snapshot="prd-af-build-remote-jobs",
        state=JobState.RUNNING,
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_completed_jobs_row_carries_branch_and_pr_url_visible_in_the_job_view():
    job = make_job()

    mark_completed(job, branch="job/job-1", pr_url="https://github.com/acme/widgets/pull/7")

    assert job.state is JobState.COMPLETED
    assert job.branch == "job/job-1"
    assert job.pr_url == "https://github.com/acme/widgets/pull/7"

    view = job_view(job)
    assert view["branch"] == "job/job-1"
    assert view["pr_url"] == "https://github.com/acme/widgets/pull/7"
    # A completed job's view never carries failure fields.
    assert "failure_reason" not in view
    assert "command_output" not in view


def test_failed_jobs_row_carries_failure_reason_and_command_output_visible_in_the_job_view():
    job = make_job()

    record_failure(job, FailureClass.SESSION_CRASHED, command_output="Traceback: API unavailable")

    assert job.state is JobState.FAILED
    assert job.failure_reason == FailureClass.SESSION_CRASHED.value
    assert job.command_output == "Traceback: API unavailable"

    view = job_view(job)
    assert view["failure_reason"] == FailureClass.SESSION_CRASHED.value
    assert view["command_output"] == "Traceback: API unavailable"
    # A failed job's view never carries the completion fields.
    assert "branch" not in view
    assert "pr_url" not in view


def test_needs_attention_jobs_row_also_carries_failure_reason_and_command_output():
    job = make_job()

    record_failure(job, FailureClass.MERGE_CONFLICT, command_output="CONFLICT (content): a.py")

    assert job.state is JobState.NEEDS_ATTENTION
    view = job_view(job)
    assert view["failure_reason"] == FailureClass.MERGE_CONFLICT.value
    assert view["command_output"] == "CONFLICT (content): a.py"


def test_a_job_still_in_flight_exposes_neither_field_pair():
    job = make_job(state=JobState.RUNNING)

    view = job_view(job)

    assert "branch" not in view
    assert "pr_url" not in view
    assert "failure_reason" not in view
    assert "command_output" not in view


# --- Wired through the real integration sequence (R33/R80) ------------------------------------


@dataclass
class Proc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class ScriptedRunner:
    merge_ok: bool = True
    merge_stderr: str = "CONFLICT (content): Merge conflict in a.py"
    calls: list = field(default_factory=list)

    def __call__(self, args, cwd, capture_output=True, text=True, check=False):
        self.calls.append((tuple(args), cwd))
        sub = args[1] if len(args) > 1 else None
        if sub == "status":
            return Proc(stdout="")
        if sub == "fetch":
            return Proc()
        if sub == "log":
            return Proc(stdout="")
        if sub == "reset":
            return Proc()
        if sub == "merge" and "--abort" not in args:
            return Proc(returncode=0 if self.merge_ok else 1, stderr="" if self.merge_ok else self.merge_stderr)
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


def test_a_successful_integration_sequence_marks_the_job_completed_with_branch_and_pr_url():
    lock = RepoIntegrationLock()
    target = make_target()
    runner = ScriptedRunner()
    job = make_job()

    result = run_integration_sequence(
        target, holder_id="holder-1", lock=lock, runner=runner,
        pr_creator=lambda t, sha: "https://github.com/acme/widgets/pull/9", job=job,
    )

    assert job.state is JobState.COMPLETED
    assert job.branch == target.job_branch
    assert job.pr_url == result.pr_url == "https://github.com/acme/widgets/pull/9"
    assert job_view(job) == {
        "id": "job-1",
        "state": "completed",
        "branch": "job/job-1",
        "pr_url": "https://github.com/acme/widgets/pull/9",
    }


def test_a_conflicting_integration_sequence_records_the_merge_output_as_command_output():
    lock = RepoIntegrationLock()
    target = make_target()
    runner = ScriptedRunner(merge_ok=False)
    job = make_job()

    try:
        run_integration_sequence(
            target, holder_id="holder-1", lock=lock, runner=runner,
            pr_creator=lambda t, sha: "unused", job=job,
        )
        raise AssertionError("expected MergeConflictError")
    except MergeConflictError:
        pass

    assert job.state is JobState.NEEDS_ATTENTION
    assert job.command_output == "CONFLICT (content): Merge conflict in a.py"
    assert job_view(job)["command_output"] == "CONFLICT (content): Merge conflict in a.py"
