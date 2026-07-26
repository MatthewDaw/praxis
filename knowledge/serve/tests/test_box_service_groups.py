"""Acceptance test for ticket R48/R49/R50 (job groups, 08d05db9):

given a job dispatched as part of a group, querying the job returns its group
identifier and querying the group returns exactly its member jobs; given a
group of three where two are terminal and one is running, no group
integration has occurred; when every member has reached a terminal state,
group integration runs exactly once over the SUCCESSFUL members, and a
member in needs-attention stays independently resumable without blocking or
aborting the batch.
"""

from __future__ import annotations

from knowledge.serve.box_service_groups import members_of_group, plan_group_integration
from knowledge.serve.box_service_models import Job, JobState


def make_job(job_id: str, state: JobState, *, group_id: str | None) -> Job:
    return Job(
        id=job_id,
        project="af-build-remote-jobs",
        snapshot="prd-af-build-remote-jobs",
        state=state,
        group_id=group_id,
    )


def test_job_carries_its_group_identifier():
    job = make_job("job-a", JobState.RUNNING, group_id="group-1")

    assert job.group_id == "group-1"


def test_querying_the_group_returns_exactly_its_member_jobs():
    a = make_job("job-a", JobState.COMPLETED, group_id="group-1")
    b = make_job("job-b", JobState.COMPLETED, group_id="group-1")
    unrelated = make_job("job-x", JobState.COMPLETED, group_id="group-2")

    members = members_of_group([a, b, unrelated], "group-1")

    assert members == [a, b]


def test_no_integration_while_a_member_is_still_open():
    finished_1 = make_job("job-1", JobState.COMPLETED, group_id="group-1")
    finished_2 = make_job("job-2", JobState.COMPLETED, group_id="group-1")
    running = make_job("job-3", JobState.RUNNING, group_id="group-1")

    decision = plan_group_integration([finished_1, finished_2, running])

    assert decision is None


def test_integration_runs_exactly_once_over_successful_members_when_all_terminal():
    finished_1 = make_job("job-1", JobState.COMPLETED, group_id="group-1")
    finished_2 = make_job("job-2", JobState.COMPLETED, group_id="group-1")
    finished_3 = make_job("job-3", JobState.COMPLETED, group_id="group-1")

    decision = plan_group_integration([finished_1, finished_2, finished_3])

    assert decision is not None
    assert decision.members == [finished_1, finished_2, finished_3]


def test_needs_attention_member_does_not_block_or_abort_the_batch():
    completed = make_job("job-1", JobState.COMPLETED, group_id="group-1")
    needs_attention = make_job("job-2", JobState.NEEDS_ATTENTION, group_id="group-1")

    decision = plan_group_integration([completed, needs_attention])

    # every member is terminal, so integration is not withheld ...
    assert decision is not None
    # ... but it runs only over the SUCCESSFUL member, excluding needs-attention
    assert decision.members == [completed]
    # and the needs-attention member is left untouched: independently resumable,
    # never marked failed or folded into the batch outcome.
    assert needs_attention.state == JobState.NEEDS_ATTENTION
    assert needs_attention.resumable is False


def test_failed_member_is_also_excluded_from_integration_but_does_not_abort_it():
    completed = make_job("job-1", JobState.COMPLETED, group_id="group-1")
    failed = make_job("job-2", JobState.FAILED, group_id="group-1")

    decision = plan_group_integration([completed, failed])

    assert decision is not None
    assert decision.members == [completed]


def test_no_successful_members_yields_a_decision_with_an_empty_integration_set():
    failed = make_job("job-1", JobState.FAILED, group_id="group-1")
    needs_attention = make_job("job-2", JobState.NEEDS_ATTENTION, group_id="group-1")

    decision = plan_group_integration([failed, needs_attention])

    assert decision is not None
    assert decision.members == []


def test_ungrouped_job_has_no_group_identifier_by_default():
    job = Job(id="solo", project="p", snapshot="prd-p", state=JobState.QUEUED)

    assert job.group_id is None
