"""R1: a job is a first-class, queryable entity with a stable id. Covers the
acceptance condition directly: querying a created job by id returns a row
whose state is one of the seven named values and no others; awaiting-human
transitions back to running without a new job being created; needs-attention
retains its artifacts (the job worktree/branch is never cleared for it); and
every terminal state carries a reason field distinct from the state value
itself."""

from __future__ import annotations

import pytest

from knowledge.serve.box_service_models import (
    Job,
    JobState,
    TERMINAL_JOB_STATES,
    mark_terminal,
)
from knowledge.serve.box_service_store import JobStore


def test_exactly_seven_named_states_and_no_others():
    assert {s.value for s in JobState} == {
        "queued",
        "claimed",
        "running",
        "awaiting-human",
        "needs-attention",
        "completed",
        "failed",
    }


def test_created_job_is_queryable_by_id_with_a_valid_state():
    store = JobStore()
    created = store.create(project="af-build-remote-jobs", snapshot="prd-af-build-remote-jobs")

    fetched = store.get(created.id)

    assert fetched is created
    assert fetched.state in set(JobState)


def test_querying_an_unknown_id_returns_no_row():
    store = JobStore()
    assert store.get("does-not-exist") is None


def test_awaiting_human_transitions_back_to_running_without_a_new_job():
    store = JobStore()
    job = store.create(project="p", snapshot="s")
    job.state = JobState.AWAITING_HUMAN

    resumed = store.resume_from_awaiting_human(job.id)

    assert resumed is job
    assert resumed.state == JobState.RUNNING
    assert len(store._jobs) == 1  # noqa: SLF001 - asserting no second row was created
    assert store.get(job.id) is resumed


def test_resume_from_awaiting_human_refuses_from_a_non_awaiting_state():
    store = JobStore()
    job = store.create(project="p", snapshot="s")  # starts queued, not awaiting-human

    with pytest.raises(ValueError):
        store.resume_from_awaiting_human(job.id)


def test_needs_attention_retains_its_artifacts():
    job = Job(
        id="job-1",
        project="p",
        snapshot="s",
        state=JobState.RUNNING,
        worktree_path="/boxes/p/job-1",
    )

    mark_terminal(job, JobState.NEEDS_ATTENTION, reason="merge-conflict")

    assert job.state == JobState.NEEDS_ATTENTION
    assert job.worktree_path == "/boxes/p/job-1"


@pytest.mark.parametrize("state", sorted(TERMINAL_JOB_STATES, key=lambda s: s.value))
def test_every_terminal_state_carries_a_reason_distinct_from_the_state(state):
    job = Job(id="job-1", project="p", snapshot="s", state=JobState.RUNNING)

    mark_terminal(job, state, reason="some-machine-readable-reason")

    assert job.state == state
    assert job.reason == "some-machine-readable-reason"
    assert job.reason != job.state
    assert job.reason != job.state.value


def test_mark_terminal_refuses_a_non_terminal_state():
    job = Job(id="job-1", project="p", snapshot="s", state=JobState.RUNNING)

    with pytest.raises(ValueError):
        mark_terminal(job, JobState.RUNNING, reason="not terminal")


def test_mark_terminal_refuses_an_empty_reason():
    job = Job(id="job-1", project="p", snapshot="s", state=JobState.RUNNING)

    with pytest.raises(ValueError):
        mark_terminal(job, JobState.FAILED, reason="")
