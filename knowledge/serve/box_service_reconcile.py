"""Restart reconciliation (R43): on box-service startup, live sessions are
reconciled against open job rows rather than left orphaned or duplicated.

Reconciliation is bidirectional (the ticket this module exists for, R68):

- An **open job row with no matching live session** — its process died while
  the service was down — is marked ``failed`` and ``resumable`` (never left
  claimed/running forever: the "duplicate execution" failure class R43 closes).
- A **live session with no matching job row** is orphaned and reaped.
- A **matched pair** (job row's ``session_id`` is among the live sessions) is
  adopted: the job row is left as-is, since the session is still doing real
  work under it.

This module is pure decision logic — no Praxis and no subprocess calls — so
the three cases from the acceptance condition are assertable without a live
CLI or database. ``apply_reconciliation`` is the thin, separately-testable
seam that turns a decision into the corresponding job-row and session action.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum

from knowledge.serve.box_service_models import Job, JobState, SessionInfo, mark_terminal


class ReconcileAction(str, Enum):
    ADOPT = "adopt"
    MARK_FAILED_RESUMABLE = "mark_failed_resumable"
    REAP = "reap"


@dataclass(frozen=True)
class ReconcileDecision:
    action: ReconcileAction
    job: Job | None = None
    session: SessionInfo | None = None


def reconcile_restart(
    open_jobs: Iterable[Job], live_sessions: Iterable[SessionInfo]
) -> list[ReconcileDecision]:
    """Decide the restart-reconciliation action for every open job row and
    every live session. ``open_jobs`` must already be filtered to open rows
    (:meth:`Job.is_open`) — this function does not filter by state so a
    caller cannot accidentally reap/adopt a job that is already at rest.
    """
    live_by_id = {session.session_id: session for session in live_sessions}
    decisions: list[ReconcileDecision] = []
    matched_session_ids: set[str] = set()

    for job in open_jobs:
        session = live_by_id.get(job.session_id) if job.session_id else None
        if session is not None:
            matched_session_ids.add(session.session_id)
            decisions.append(ReconcileDecision(ReconcileAction.ADOPT, job=job, session=session))
        else:
            decisions.append(ReconcileDecision(ReconcileAction.MARK_FAILED_RESUMABLE, job=job))

    for session in live_by_id.values():
        if session.session_id not in matched_session_ids:
            decisions.append(ReconcileDecision(ReconcileAction.REAP, session=session))

    return decisions


#: Machine-readable reason stamped on a job marked failed+resumable by restart
#: reconciliation, distinct from any in-run failure class (see
#: ``box_service_failures.FailureClass``).
SESSION_MISSING_AT_RESTART = "session_missing_at_restart"


def apply_reconciliation(
    decisions: Iterable[ReconcileDecision],
    *,
    terminate: Callable[[str], bool],
) -> list[Job]:
    """Apply each decision. Adopted jobs are returned unchanged. A
    mark-failed-resumable job row is mutated in place to ``failed`` /
    ``resumable=True`` with a distinct reason and returned. A reap decision
    calls ``terminate`` (the session-launcher seam) on the orphaned session
    id and is not reflected in the returned list, since it has no job row.
    """
    reconciled: list[Job] = []
    for decision in decisions:
        if decision.action is ReconcileAction.ADOPT:
            reconciled.append(decision.job)  # type: ignore[arg-type]
        elif decision.action is ReconcileAction.MARK_FAILED_RESUMABLE:
            job = decision.job
            assert job is not None
            mark_terminal(job, JobState.FAILED, SESSION_MISSING_AT_RESTART)
            job.resumable = True
            reconciled.append(job)
        elif decision.action is ReconcileAction.REAP:
            session = decision.session
            assert session is not None
            terminate(session.session_id)
    return reconciled
