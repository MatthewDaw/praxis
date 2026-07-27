"""Cancel action (R77): the operator can cancel a stuck-but-alive job from the website and from
MCP, terminating its background session while preserving the job's evidence and inspection
artifacts.

Cancel is the operator-triggered control action for a job that is open (queued, claimed, running,
or awaiting-human — see ``box_service_models.OPEN_JOB_STATES``) but has stopped producing visible
progress — "stuck but alive". Unlike a failure class (``box_service_failures``) or restart
reconciliation's orphan reap (``box_service_reconcile``), cancel is always operator-initiated: the
job row lands on ``NEEDS_ATTENTION`` with a distinct reason, ``operator-cancelled``, so it is never
confused with a failure the box service itself detected. The job's worktree (and, since nothing
here touches git, its branch) are preserved for inspection — ``mark_terminal``'s
``clear_worktree=False`` default, same as every other needs-attention path (R34).

Ordering matters (R41's guarantee, which this action must uphold too): the final activity tail and
terminal event are persisted BEFORE the session is torn down, so a cancelled job's evidence outlives
the session that produced it. ``cancel_job`` enforces that ordering itself — persist, then
terminate — rather than trusting each caller to sequence it correctly.

``cancel_job`` is the single function both the website handler and the MCP tool call (mirroring
``box_service_resume.resume_job``'s "callable identically from either surface" shape) — neither
surface duplicates the ordering or the state transition.
"""

from __future__ import annotations

from collections.abc import Callable

from knowledge.serve.box_service_models import OPEN_JOB_STATES, Job, JobState, mark_terminal

#: Machine-readable reason stamped on a job the operator cancelled (R77) — distinct from any
#: in-run failure class (``box_service_failures.FailureClass``) or restart-reconciliation reason
#: (``box_service_reconcile.SESSION_MISSING_AT_RESTART``): cancel is always operator-initiated,
#: never inferred from an observed failure.
OPERATOR_CANCELLED = "operator-cancelled"

#: Persist the final activity tail and terminal event for ``job``. The real implementation writes
#: both to durable storage; injected here so the persist-before-teardown ordering is assertable
#: without any I/O.
PersistTail = Callable[[Job], None]

#: Terminate the job's background session by CLI session id — the same session-launcher seam
#: ``box_service_reconcile.apply_reconciliation`` uses (``SessionLauncher.terminate``).
Terminate = Callable[[str], bool]


class CancelError(RuntimeError):
    """Raised when cancel is attempted on a job with no live session to cancel."""


def can_cancel(job: Job) -> bool:
    """True iff ``job`` is open (R1's ``Job.is_open``) AND has a live session recorded — a
    "stuck-but-alive" job, the only kind cancel applies to. A queued job with no session yet, or a
    job already at rest, has nothing running to terminate."""
    return job.state in OPEN_JOB_STATES and job.session_id is not None


def cancel_job(job: Job, *, persist_tail: PersistTail, terminate: Terminate) -> Job:
    """Cancel ``job`` (the operator-triggered control action; callable identically from the website
    handler and the MCP tool — both are thin callers of this one function).

    Refuses (raises :class:`CancelError`) when ``job`` has no live session to cancel — already at
    rest, or never launched. On success: persists the final activity tail and terminal event BEFORE
    tearing the session down (the ordering is enforced here, not left to the caller), terminates the
    session, and marks the job ``NEEDS_ATTENTION`` with reason ``operator-cancelled`` — never
    clearing ``worktree_path``, so the job's branch and worktree remain for inspection.
    """
    if not can_cancel(job):
        raise CancelError(f"job {job.id} has no live session to cancel")

    persist_tail(job)
    terminate(job.session_id)  # type: ignore[arg-type]  # can_cancel guarantees session_id is set
    mark_terminal(job, JobState.NEEDS_ATTENTION, OPERATOR_CANCELLED)
    return job
