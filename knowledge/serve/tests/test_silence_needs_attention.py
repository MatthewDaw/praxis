"""Acceptance test for R78 (38857e4be8464497ad596a2645a2d8a5): a job whose
last-activity exceeds a stated multiple of the silence threshold transitions
to needs-attention with reason silent and becomes eligible for the backstop
reaper, and the identical rule applies to a derived local job whose run
markers have gone stale past that threshold, so a stall reads the same way
in either venue.
"""

from __future__ import annotations

from knowledge.serve.box_service_activity import (
    NEEDS_ATTENTION_SILENCE_THRESHOLD_S,
    SILENCE_THRESHOLD_S,
    is_reaper_eligible_for_silence,
    silence_needs_attention_view,
)
from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.local_derived_job import DEFAULT_RUN_TTL_S, LocalJobState, derive_local_job

NOW = 10_000_000.0
ORG = "org-a"


def _remote_job(**overrides) -> Job:
    defaults = dict(
        id="job-1",
        project="proj-a",
        snapshot="prd-proj-a",
        state=JobState.RUNNING,
        session_id="sess-1",
        run_owner="box-1",
        org=ORG,
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_remote_job_silent_past_the_multiple_reads_needs_attention_and_is_reaper_eligible():
    job = _remote_job(last_activity_at=NOW - NEEDS_ATTENTION_SILENCE_THRESHOLD_S - 1)

    assert is_reaper_eligible_for_silence(job, now=NOW) is True
    view = silence_needs_attention_view(job, now=NOW)
    assert view["state"] == JobState.NEEDS_ATTENTION.value
    assert view["reason"] == "silent"
    assert view["reaper_eligible"] is True


def test_remote_job_only_past_the_bare_silence_threshold_is_not_yet_reaper_eligible():
    # Silent (R22's is_silent) but not yet past the stated MULTIPLE -- still
    # in the grace window, not yet needs-attention/reaper-eligible.
    job = _remote_job(last_activity_at=NOW - SILENCE_THRESHOLD_S - 1, state=JobState.RUNNING)

    assert is_reaper_eligible_for_silence(job, now=NOW) is False
    view = silence_needs_attention_view(job, now=NOW)
    assert view["state"] == JobState.RUNNING.value
    assert view["reaper_eligible"] is False


def test_remote_job_with_fresh_activity_is_never_reaper_eligible():
    job = _remote_job(last_activity_at=NOW - 5)

    assert is_reaper_eligible_for_silence(job, now=NOW) is False


def _ticket(*, run_owner: str | None, run_at: float | None, build_state: str = "in_progress") -> dict:
    return {"id": "cid", "meta": {"run_owner": run_owner, "run_at": run_at, "build_state": build_state}}


def test_local_derived_job_stale_past_the_same_threshold_reads_needs_attention_and_silent():
    assert DEFAULT_RUN_TTL_S == NEEDS_ATTENTION_SILENCE_THRESHOLD_S  # the two venues share one window
    stale_at = NOW - DEFAULT_RUN_TTL_S - 1
    tickets = [
        _ticket(run_owner="owner-1", run_at=stale_at, build_state="finished"),
        _ticket(run_owner="owner-1", run_at=stale_at, build_state="in_progress"),
    ]

    job = derive_local_job(tickets, now=NOW)

    assert job.state is LocalJobState.NEEDS_ATTENTION
    assert job.reason == "silent"


def test_local_derived_job_fresh_run_marker_is_not_needs_attention():
    tickets = [_ticket(run_owner="owner-1", run_at=NOW - 60)]

    job = derive_local_job(tickets, now=NOW)

    assert job.state is LocalJobState.RUNNING
    assert job.reason is None
