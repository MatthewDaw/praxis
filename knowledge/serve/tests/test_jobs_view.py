"""Unit tests for box_service_jobs_view (R26): the jobs-view ordering."""

from __future__ import annotations

from knowledge.serve.box_service_jobs_view import (
    ATTENTION_STATES,
    JobViewRow,
    needs_attention,
    order_jobs_for_view,
)
from knowledge.serve.box_service_models import Job, JobState

NOW = 10_000.0


def _job(job_id: str, state: JobState, **kwargs) -> Job:
    return Job(id=job_id, project="p", snapshot="s", state=state, **kwargs)


def test_awaiting_human_always_needs_attention():
    job = _job("j1", JobState.AWAITING_HUMAN)
    assert needs_attention(job, now=NOW) is True


def test_failed_always_needs_attention():
    job = _job("j1", JobState.FAILED)
    assert needs_attention(job, now=NOW) is True


def test_needs_attention_state_needs_attention():
    job = _job("j1", JobState.NEEDS_ATTENTION)
    assert needs_attention(job, now=NOW) is True


def test_running_job_with_fresh_heartbeat_does_not_need_attention():
    job = _job("j1", JobState.RUNNING, claim_heartbeat_at=NOW - 10)
    assert needs_attention(job, now=NOW) is False


def test_running_job_silent_past_threshold_needs_attention():
    job = _job("j1", JobState.RUNNING, claim_heartbeat_at=NOW - 901)
    assert needs_attention(job, now=NOW) is True


def test_queued_job_falls_back_to_queued_at_for_staleness():
    stale = _job("j1", JobState.QUEUED, queued_at=NOW - 901)
    fresh = _job("j2", JobState.QUEUED, queued_at=NOW - 10)
    assert needs_attention(stale, now=NOW) is True
    assert needs_attention(fresh, now=NOW) is False


def test_completed_job_never_needs_attention_regardless_of_staleness():
    job = _job("j1", JobState.COMPLETED, claim_heartbeat_at=NOW - 100_000)
    assert needs_attention(job, now=NOW) is False


def test_open_job_with_no_observed_timestamp_does_not_need_attention():
    job = _job("j1", JobState.CLAIMED)
    assert needs_attention(job, now=NOW) is False


def test_order_jobs_for_view_places_every_attention_needing_job_above_every_normal_one():
    # A deliberately interleaved mix: attention-needing and progressing-normally jobs,
    # so a naive "already sorted" input can't accidentally pass.
    progressing_1 = _job("progressing-1", JobState.RUNNING, claim_heartbeat_at=NOW - 5)
    attention_1 = _job("attention-1", JobState.AWAITING_HUMAN)
    progressing_2 = _job("progressing-2", JobState.QUEUED, queued_at=NOW - 1)
    attention_2 = _job("attention-2", JobState.FAILED)
    progressing_3 = _job("progressing-3", JobState.CLAIMED, claim_heartbeat_at=NOW - 2)
    attention_3 = _job("attention-3", JobState.RUNNING, claim_heartbeat_at=NOW - 1000)

    jobs = [progressing_1, attention_1, progressing_2, attention_2, progressing_3, attention_3]
    rows = order_jobs_for_view(jobs, now=NOW)

    assert [r.job.id for r in rows] == [
        "attention-1",
        "attention-2",
        "attention-3",
        "progressing-1",
        "progressing-2",
        "progressing-3",
    ]
    # Every attention-needing row genuinely precedes every normal row (the acceptance
    # condition's literal claim), asserted independent of the exact ordering above.
    attention_idxs = [i for i, r in enumerate(rows) if r.needs_attention]
    normal_idxs = [i for i, r in enumerate(rows) if not r.needs_attention]
    assert max(attention_idxs) < min(normal_idxs)


def test_order_jobs_for_view_preserves_relative_order_within_each_group():
    a1 = _job("a1", JobState.FAILED)
    a2 = _job("a2", JobState.AWAITING_HUMAN)
    n1 = _job("n1", JobState.RUNNING, claim_heartbeat_at=NOW)
    n2 = _job("n2", JobState.QUEUED, queued_at=NOW)

    rows = order_jobs_for_view([n1, a1, n2, a2], now=NOW)

    assert [r.job.id for r in rows] == ["a1", "a2", "n1", "n2"]


def test_job_view_row_carries_the_job_and_its_flag():
    job = _job("j1", JobState.FAILED)
    row = JobViewRow(job=job, needs_attention=True)
    assert row.job is job
    assert row.needs_attention is True


def test_attention_states_are_exactly_awaiting_human_failed_needs_attention():
    assert ATTENTION_STATES == {
        JobState.AWAITING_HUMAN,
        JobState.FAILED,
        JobState.NEEDS_ATTENTION,
    }
