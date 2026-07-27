"""Terminal-event reconciliation (R24 / AE9): a job's terminal moment is
captured as a discrete event rather than inferred from a poll interval, and
session exit alone never means the work finished — af-build's gate blocks
session end until tickets pass, so exit must be reconciled against ticket
completeness (:func:`box_service_scope.job_scope_complete`) before the job's
terminal state is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass

from knowledge.serve.box_service_failures import FailureClass, record_failure
from knowledge.serve.box_service_models import Job, JobState, mark_terminal
from knowledge.serve.box_service_scope import job_scope_complete

#: Machine-readable reason stamped when a terminal event is reconciled against
#: a fully complete job scope — distinct from any failure class's reason, so a
#: completed job's "why" is recorded as deliberately as a failed one's (R1).
JOB_SCOPE_COMPLETE_REASON = "job_scope_complete"


@dataclass(frozen=True)
class TerminalEvent:
    """A harness-fired terminal event (R24). ``occurred_at`` is the event's
    own timestamp — the sole source :func:`reconcile_terminal_event` stamps
    ``Job.terminal_at`` from, never a wall-clock read taken whenever the event
    happens to be processed (which is what an externally polled session
    listing would otherwise force)."""

    session_id: str
    occurred_at: float


def reconcile_terminal_event(
    job: Job, event: TerminalEvent, requirement_facts: list[dict]
) -> Job:
    """Reconcile a session's discrete terminal event against ticket
    completeness and record ``job``'s terminal state, in place (R24 / AE9).

    Every in-scope requirement finished or blocked -> ``completed``. Any
    in-scope requirement still open -> ``failed`` via
    ``FailureClass.TICKETS_INCOMPLETE_AT_EXIT`` — the "silent partial
    failure" class this reconciliation exists to close, never silently
    recorded as completed just because the session exited.

    ``job.terminal_at`` is stamped from ``event.occurred_at`` in both
    branches, so the recorded terminal timestamp always comes from the
    discrete event, never a poll.
    """
    if job_scope_complete(requirement_facts):
        mark_terminal(job, JobState.COMPLETED, JOB_SCOPE_COMPLETE_REASON)
    else:
        record_failure(job, FailureClass.TICKETS_INCOMPLETE_AT_EXIT)
    job.terminal_at = event.occurred_at
    return job
