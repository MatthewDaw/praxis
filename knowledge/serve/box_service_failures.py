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

The six classes in ``RETRYABLE_FAILURE_CLASSES`` are the operational failures
named by the "return to queued at most 3 times" acceptance floor (clone/fetch,
session launch, notification send, pull-request open, credential, worktree
deletion): rather than going straight to a terminal state, ``record_failure``
sends the job back to ``queued`` for another automatic attempt while it is
still under ``Job.max_attempts``, and only lands on the terminal
``needs-attention`` state — carrying the failure reason and the underlying
error text — once the bound is reached, so automatic re-queueing stops for
good.
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
    #: R33: the main worktree is dirty or holds a commit not yet published to
    #: the PR base at integration time. The work already sitting in the main
    #: worktree is preserved for a human rather than silently reset away, so
    #: this is needs-attention, not failed.
    MAIN_WORKTREE_DIRTY = "main_worktree_dirty"
    #: R62: on replay after a crash, the job's durable delivery stage does not
    #: reconcile with the re-detected remote state (e.g. the stage claims the
    #: branch was published but it is missing). Never guessed or retried
    #: blind — the branch is preserved for a human, so this is
    #: needs-attention, not failed.
    DELIVERY_STAGE_UNRECONCILABLE = "delivery_stage_unreconcilable"
    #: The box's per-repo clone/fetch step failed (R10) — transient network or
    #: origin trouble, worth an automatic retry.
    CLONE_FETCH_FAILED = "clone_fetch_failed"
    #: Launching the background Claude Code session failed (R13) — worth an
    #: automatic retry before treating it as a capability regression.
    SESSION_LAUNCH_FAILED = "session_launch_failed"
    #: Delivering the operator notification failed (R27) — a transport hiccup,
    #: not a reason to abandon the job.
    NOTIFICATION_SEND_FAILED = "notification_send_failed"
    #: Opening the pull request after a successful build failed — the branch
    #: exists, so this is worth retrying before it needs a human.
    PULL_REQUEST_OPEN_FAILED = "pull_request_open_failed"
    #: The box's push credential could not be obtained or used (R36/R37) —
    #: often a transient secrets-manager/assumed-role hiccup.
    CREDENTIAL_FAILED = "credential_failed"
    #: Deleting a job worktree after integration failed (R40) — retry before
    #: leaving the orphaned tree for a human to clear.
    WORKTREE_DELETION_FAILED = "worktree_deletion_failed"


#: The terminal JobState each failure class transitions a job to once it is
#: no longer retryable (immediately, for the five original classes; once
#: ``Job.max_attempts`` is reached, for the ``RETRYABLE_FAILURE_CLASSES``).
TERMINAL_STATE_FOR_CLASS: dict[FailureClass, JobState] = {
    FailureClass.TICKETS_INCOMPLETE_AT_EXIT: JobState.FAILED,
    FailureClass.SESSION_CRASHED: JobState.FAILED,
    FailureClass.MERGE_CONFLICT: JobState.NEEDS_ATTENTION,
    FailureClass.CAPABILITY_PROBE_FAILED: JobState.FAILED,
    FailureClass.MAIN_WORKTREE_DIRTY: JobState.NEEDS_ATTENTION,
    FailureClass.DELIVERY_STAGE_UNRECONCILABLE: JobState.NEEDS_ATTENTION,
    FailureClass.CLONE_FETCH_FAILED: JobState.NEEDS_ATTENTION,
    FailureClass.SESSION_LAUNCH_FAILED: JobState.NEEDS_ATTENTION,
    FailureClass.NOTIFICATION_SEND_FAILED: JobState.NEEDS_ATTENTION,
    FailureClass.PULL_REQUEST_OPEN_FAILED: JobState.NEEDS_ATTENTION,
    FailureClass.CREDENTIAL_FAILED: JobState.NEEDS_ATTENTION,
    FailureClass.WORKTREE_DELETION_FAILED: JobState.NEEDS_ATTENTION,
}

#: Failure classes that get automatic retries (a return to ``queued``) rather
#: than an immediate terminal state — see the acceptance floor quoted in the
#: module docstring.
RETRYABLE_FAILURE_CLASSES: frozenset[FailureClass] = frozenset(
    {
        FailureClass.CLONE_FETCH_FAILED,
        FailureClass.SESSION_LAUNCH_FAILED,
        FailureClass.NOTIFICATION_SEND_FAILED,
        FailureClass.PULL_REQUEST_OPEN_FAILED,
        FailureClass.CREDENTIAL_FAILED,
        FailureClass.WORKTREE_DELETION_FAILED,
    }
)


def record_failure(job: Job, failure_class: FailureClass, error_text: str | None = None) -> Job:
    """Record ``failure_class`` against ``job``, in place, with the
    underlying ``error_text`` (if any).

    Always increments ``attempt_count`` and stamps ``failure_reason`` /
    ``error_text``. For a class in ``RETRYABLE_FAILURE_CLASSES`` still under
    ``Job.max_attempts``, sends the job back to ``queued`` for another
    automatic attempt (``resumable=True``) instead of a terminal state.
    Otherwise — the original non-retryable classes, or a retryable one whose
    attempt bound is now reached — transitions to the class's terminal state
    and sets ``resumable`` to whether the job is still under its attempt
    bound, which is what stops automatic re-queueing for good.
    """
    job.attempt_count += 1
    job.error_text = error_text
    if failure_class in RETRYABLE_FAILURE_CLASSES and job.attempt_count < job.max_attempts:
        job.state = JobState.QUEUED
        job.failure_reason = failure_class.value
        job.resumable = True
        return job
    mark_terminal(job, TERMINAL_STATE_FOR_CLASS[failure_class], failure_class.value)
    job.resumable = job.attempt_count < job.max_attempts
    return job
