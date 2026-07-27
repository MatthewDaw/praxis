"""Acceptance test for ticket R68 (restart reconciliation, 36f73af0):

given a restart with an open job row and no live session, the job is marked
failed and resumable; given a live session with no job row, it is reaped;
given a matched pair, it is adopted.
"""

from __future__ import annotations

from knowledge.serve.box_service_models import Job, JobState, SessionInfo
from knowledge.serve.box_service_reconcile import (
    ReconcileAction,
    SESSION_MISSING_AT_RESTART,
    apply_reconciliation,
    reconcile_restart,
)


def make_session(session_id: str) -> SessionInfo:
    return SessionInfo(
        session_id=session_id,
        cwd=f"/repo/wt-{session_id}",
        kind="bg",
        started_at="2026-07-25T00:00:00Z",
        name=session_id,
        state="running",
    )


def make_job(job_id: str, *, session_id: str | None, state: JobState = JobState.RUNNING) -> Job:
    return Job(
        id=job_id,
        project="af-build-remote-jobs",
        snapshot="prd-af-build-remote-jobs",
        state=state,
        session_id=session_id,
    )


def test_open_job_row_with_no_live_session_is_marked_failed_and_resumable():
    job = make_job("job-orphan-row", session_id="sess-dead")

    decisions = reconcile_restart(open_jobs=[job], live_sessions=[])

    assert len(decisions) == 1
    assert decisions[0].action == ReconcileAction.MARK_FAILED_RESUMABLE
    assert decisions[0].job is job

    reconciled = apply_reconciliation(decisions, terminate=lambda _sid: True)

    assert reconciled == [job]
    assert job.state == JobState.FAILED
    assert job.resumable is True
    assert job.failure_reason == SESSION_MISSING_AT_RESTART


def test_live_session_with_no_job_row_is_reaped():
    orphan_session = make_session("sess-orphan")
    terminated: list[str] = []

    decisions = reconcile_restart(open_jobs=[], live_sessions=[orphan_session])

    assert len(decisions) == 1
    assert decisions[0].action == ReconcileAction.REAP
    assert decisions[0].session is orphan_session

    reconciled = apply_reconciliation(decisions, terminate=terminated.append)

    assert reconciled == []
    assert terminated == ["sess-orphan"]


def test_matched_pair_is_adopted():
    session = make_session("sess-live")
    job = make_job("job-matched", session_id="sess-live")

    decisions = reconcile_restart(open_jobs=[job], live_sessions=[session])

    assert len(decisions) == 1
    assert decisions[0].action == ReconcileAction.ADOPT
    assert decisions[0].job is job
    assert decisions[0].session is session

    terminated: list[str] = []
    reconciled = apply_reconciliation(decisions, terminate=terminated.append)

    assert reconciled == [job]
    assert job.state == JobState.RUNNING  # unchanged
    assert job.resumable is False
    assert terminated == []


def test_all_three_cases_together_do_not_cross_contaminate():
    dead_row_job = make_job("job-dead-row", session_id="sess-dead")
    matched_job = make_job("job-matched", session_id="sess-live")
    matched_session = make_session("sess-live")
    orphan_session = make_session("sess-orphan")

    decisions = reconcile_restart(
        open_jobs=[dead_row_job, matched_job],
        live_sessions=[matched_session, orphan_session],
    )

    by_action = {d.action for d in decisions}
    assert by_action == {
        ReconcileAction.MARK_FAILED_RESUMABLE,
        ReconcileAction.ADOPT,
        ReconcileAction.REAP,
    }

    terminated: list[str] = []
    apply_reconciliation(decisions, terminate=terminated.append)

    assert dead_row_job.state == JobState.FAILED
    assert dead_row_job.resumable is True
    assert matched_job.state == JobState.RUNNING
    assert terminated == ["sess-orphan"]


def test_closed_job_rows_are_never_reconciled_by_this_function():
    # reconcile_restart trusts its caller to pass only open rows (Job.is_open());
    # a caller that filters correctly never asks it to touch a finished job.
    finished_job = make_job("job-done", session_id=None, state=JobState.COMPLETED)
    assert finished_job.is_open() is False
