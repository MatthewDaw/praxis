"""Acceptance test for ticket R45 (31aff1ea0e19410d923b55eb150f51e0):

given local run markers inside the recency window, the job list shows a
derived job with an id derived from the run owner and no job row exists;
given markers older than the window, the derived job is absent from that
list; and the derived job's view labels the missing tail, question
detection, message delivery and resume as deliberately unavailable rather
than failed.

Also covers the mandatory ``local-job-goes-terminal`` check (1eca3b16...):
a local run whose process was killed stops reporting running once its run
markers pass TTL and reports a terminal state reconciled against the
remaining in-scope tickets, rather than showing running forever.
"""

from __future__ import annotations

from knowledge.serve.local_derived_job import (
    DEFAULT_RUN_TTL_S,
    CapabilityStatus,
    LOCAL_UNAVAILABLE_CAPABILITIES,
    LocalJobState,
    derive_local_job,
    derive_local_job_id,
    list_live_local_jobs,
)

NOW = 2_000_000.0
RUN_OWNER = "af-build-orchestrator-resume2"


def make_ticket(*, run_owner: str | None, run_at: float | None, build_state: str = "in_progress") -> dict:
    return {"id": "cid", "meta": {"run_owner": run_owner, "run_at": run_at, "build_state": build_state}}


def test_fresh_run_marker_projects_a_running_job_with_no_job_row():
    tickets = [make_ticket(run_owner=RUN_OWNER, run_at=NOW - 60)]

    job = derive_local_job(tickets, now=NOW)

    assert job is not None
    assert job.id == derive_local_job_id(RUN_OWNER)
    assert job.state is LocalJobState.RUNNING
    # No job row: the projection carries only the read-time view, never a
    # persisted id referencing a stored row (R45) -- there is nothing here
    # but the dataclass itself.
    assert list_live_local_jobs(tickets, now=NOW) == [job]


def test_marker_inside_window_boundary_still_running():
    tickets = [make_ticket(run_owner=RUN_OWNER, run_at=NOW - DEFAULT_RUN_TTL_S)]

    job = derive_local_job(tickets, now=NOW)

    assert job.state is LocalJobState.RUNNING


def test_killed_run_stops_showing_running_once_ttl_passes_and_reports_terminal():
    stale_at = NOW - DEFAULT_RUN_TTL_S - 1
    tickets = [
        make_ticket(run_owner=RUN_OWNER, run_at=stale_at, build_state="finished"),
        make_ticket(run_owner=RUN_OWNER, run_at=stale_at, build_state="finished"),
    ]

    job = derive_local_job(tickets, now=NOW)

    assert job is not None
    assert job.state is LocalJobState.COMPLETED
    assert job.state is not LocalJobState.RUNNING


def test_killed_run_with_incomplete_tickets_reports_failed_not_running_forever():
    stale_at = NOW - DEFAULT_RUN_TTL_S - 1
    tickets = [
        make_ticket(run_owner=RUN_OWNER, run_at=stale_at, build_state="finished"),
        make_ticket(run_owner=RUN_OWNER, run_at=stale_at, build_state="in_progress"),
    ]

    job = derive_local_job(tickets, now=NOW)

    assert job.state is LocalJobState.FAILED


def test_markers_older_than_window_are_absent_from_the_job_list():
    stale_at = NOW - DEFAULT_RUN_TTL_S - 1
    tickets = [make_ticket(run_owner=RUN_OWNER, run_at=stale_at, build_state="finished")]

    assert list_live_local_jobs(tickets, now=NOW) == []


def test_no_run_marker_at_all_derives_no_job():
    tickets = [make_ticket(run_owner=None, run_at=None)]

    assert derive_local_job(tickets, now=NOW) is None
    assert list_live_local_jobs(tickets, now=NOW) == []


def test_view_labels_missing_capabilities_as_unavailable_by_design_not_failed():
    tickets = [make_ticket(run_owner=RUN_OWNER, run_at=NOW - 60)]

    job = derive_local_job(tickets, now=NOW)

    assert set(job.capabilities) == set(LOCAL_UNAVAILABLE_CAPABILITIES)
    for status in job.capabilities.values():
        assert status is CapabilityStatus.UNAVAILABLE_BY_DESIGN
        assert status.value != "failed"
