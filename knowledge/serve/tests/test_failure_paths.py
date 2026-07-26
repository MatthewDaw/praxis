"""Each named failure class transitions the job to a recorded failed or
needs-attention state with a distinct machine-readable reason, increments the
attempt count, and the attempt bound stops automatic re-queueing (the
``failure-handling`` check this file satisfies)."""

from __future__ import annotations

import pytest

from knowledge.serve.box_service_failures import FailureClass, record_failure
from knowledge.serve.box_service_models import Job, JobState


def make_job(**overrides) -> Job:
    defaults = dict(
        id="job-1",
        project="af-build-remote-jobs",
        snapshot="prd-af-build-remote-jobs",
        state=JobState.RUNNING,
    )
    defaults.update(overrides)
    return Job(**defaults)


@pytest.mark.parametrize(
    "failure_class,expected_state",
    [
        (FailureClass.TICKETS_INCOMPLETE_AT_EXIT, JobState.FAILED),
        (FailureClass.SESSION_CRASHED, JobState.FAILED),
        (FailureClass.MERGE_CONFLICT, JobState.NEEDS_ATTENTION),
        (FailureClass.CAPABILITY_PROBE_FAILED, JobState.FAILED),
    ],
)
def test_each_failure_class_records_its_terminal_state_and_reason(failure_class, expected_state):
    job = make_job()

    record_failure(job, failure_class)

    assert job.state == expected_state
    assert job.failure_reason == failure_class.value


def test_failure_classes_use_distinct_reasons():
    reasons = {fc.value for fc in FailureClass}
    assert len(reasons) == len(FailureClass)


def test_attempt_count_increments_on_each_failure():
    job = make_job(max_attempts=5)

    record_failure(job, FailureClass.SESSION_CRASHED)
    assert job.attempt_count == 1
    record_failure(job, FailureClass.SESSION_CRASHED)
    assert job.attempt_count == 2


def test_resumable_while_under_attempt_bound():
    job = make_job(max_attempts=3, attempt_count=1)

    record_failure(job, FailureClass.SESSION_CRASHED)

    assert job.attempt_count == 2
    assert job.resumable is True


def test_attempt_bound_stops_automatic_requeue():
    job = make_job(max_attempts=3, attempt_count=2)

    record_failure(job, FailureClass.SESSION_CRASHED)

    assert job.attempt_count == 3
    assert job.resumable is False


def test_needs_attention_failure_also_respects_attempt_bound():
    job = make_job(max_attempts=1, attempt_count=0)

    record_failure(job, FailureClass.MERGE_CONFLICT)

    assert job.state == JobState.NEEDS_ATTENTION
    assert job.resumable is False
