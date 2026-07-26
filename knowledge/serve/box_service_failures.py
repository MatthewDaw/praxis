"""Named failure classes for a running job (the ``failure-handling`` tag's
``failure-paths`` check), distinct from restart reconciliation
(``box_service_reconcile``) which handles the box-service-was-down case.

Each class transitions the job to a recorded terminal state — ``failed`` for
a class that leaves nothing worth preserving by hand, ``needs-attention`` for
one that does (R34) — with a distinct machine-readable reason, and increments
the attempt count. Once the attempt count reaches the job's bound, the job is
no longer marked resumable, which is what stops automatic re-queueing; the
box service is expected to check ``Job.resumable`` before requeuing rather
than requeue unconditionally on every failure.
"""

from __future__ import annotations

from enum import Enum

from knowledge.serve.box_service_models import Job, JobState, mark_terminal


class FailureClass(str, Enum):
    """Failure classes drawn from the plan's "Edge States and Failure
    Classes" section (docs/brainstorms/2026-07-24-af-build-remote-jobs-requirements.md)."""

    #: R24 "silent partial failure": session exited but tickets are
    #: incomplete — must record failed, never completed.
    TICKETS_INCOMPLETE_AT_EXIT = "tickets_incomplete_at_exit"
    #: The process died mid-turn, e.g. the Anthropic API became unavailable.
    SESSION_CRASHED = "session_crashed"
    #: R34: the job branch conflicts merging into the main worktree. The
    #: branch is preserved for a human, so this is needs-attention, not failed.
    MERGE_CONFLICT = "merge_conflict"
    #: R17: a startup capability probe failed, so the box service refuses to
    #: run this job rather than degrade silently.
    CAPABILITY_PROBE_FAILED = "capability_probe_failed"


#: The terminal JobState each failure class transitions a job to.
TERMINAL_STATE_FOR_CLASS: dict[FailureClass, JobState] = {
    FailureClass.TICKETS_INCOMPLETE_AT_EXIT: JobState.FAILED,
    FailureClass.SESSION_CRASHED: JobState.FAILED,
    FailureClass.MERGE_CONFLICT: JobState.NEEDS_ATTENTION,
    FailureClass.CAPABILITY_PROBE_FAILED: JobState.FAILED,
}


def record_failure(job: Job, failure_class: FailureClass) -> Job:
    """Transition ``job`` to its failure class's terminal state, in place.

    Increments ``attempt_count`` and sets ``resumable`` to whether the job is
    still under its attempt bound — the attempt bound is what stops automatic
    re-queueing, not a special-cased state.
    """
    job.attempt_count += 1
    mark_terminal(job, TERMINAL_STATE_FOR_CLASS[failure_class], failure_class.value)
    job.resumable = job.attempt_count < job.max_attempts
    return job
