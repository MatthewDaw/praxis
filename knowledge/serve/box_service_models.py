"""Shared data model for the box service's remote-job machinery.

A **job** is the unit the box service claims, executes, observes, and cleans
up (see ``docs/brainstorms/2026-07-24-af-build-remote-jobs-requirements.md``).
This module carries only the plain data shapes shared by the box-service
building blocks (session launching, restart reconciliation, failure
classification) that land across several tickets tagged ``box-service`` /
``session-lifecycle`` / ``failure-handling`` — it owns no I/O and no Praxis or
subprocess dependency, so every consumer can unit test against it directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class JobState(str, Enum):
    """A job's lifecycle state (R1). ``NEEDS_ATTENTION`` is the terminal state
    for a failure class that preserves work for a human rather than one that
    is cleanly resumable (see R34, ``failure-paths`` check)."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting-human"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs-attention"


#: States in which a job row is still "open" — i.e. not yet at rest — and so
#: is a candidate for restart reconciliation (R43).
OPEN_JOB_STATES = frozenset(
    {JobState.QUEUED, JobState.CLAIMED, JobState.RUNNING, JobState.AWAITING_HUMAN}
)

#: The at-rest states (R1). Every one of these carries a machine-readable
#: reason (``Job.failure_reason``) distinct from the state value itself —
#: including ``COMPLETED``, so a completed job's "why" is recorded the same
#: way a failed one's is.
TERMINAL_JOB_STATES = frozenset(
    {JobState.COMPLETED, JobState.FAILED, JobState.NEEDS_ATTENTION}
)


def mark_terminal(job: "Job", state: JobState, reason: str, *, clear_worktree: bool = False) -> "Job":
    """Transition ``job`` to a terminal ``state`` with a ``reason`` kept in a
    field distinct from the state itself (R1). ``clear_worktree`` defaults to
    False so a needs-attention job's worktree/branch artifacts are retained
    for a human (R34); a caller that has safely torn down the worktree after
    a clean completed/failed merge may pass ``clear_worktree=True``.
    """
    if state not in TERMINAL_JOB_STATES:
        raise ValueError(f"{state.value!r} is not a terminal JobState")
    job.state = state
    job.failure_reason = reason
    if clear_worktree:
        job.worktree_path = None
    return job


@dataclass
class Lease:
    """A holder id + heartbeat + expiry (R2/R63): every lease in the system —
    the job-claim lease, the job-control lease resume takes, and the host
    advisory lock — carries this shape, so a dead holder never permanently
    strands the resource it held (the caller checks ``is_live`` against a
    fresh ``now`` before ever treating a stale lease as still enforced)."""

    holder_id: str
    heartbeat_at: float
    expires_at: float

    def is_live(self, now: float) -> bool:
        return now < self.expires_at


@dataclass
class Job:
    """A box-service job row (R1, R31). ``session_id`` and ``run_owner`` are
    ``None`` until the job has been launched at least once. ``group_id`` is
    ``None`` for a solo job; when set, it is explicit group membership (R50)
    that ``box_service_groups`` uses to find a job's batch and decide when
    the batch's barrier opens (R48). ``org`` scopes the row for cross-org
    read/write authorization (R-ticket a75ca6a9); ``claim_lease`` is who
    currently owns terminal-write/mailbox/resume authority over the job.

    ``run_owner`` doubles as the claim lease's holder id (R2); ``claim_heartbeat_at``
    and ``claim_lease_ttl`` are the heartbeat and expiry that, together with the
    holder id, make the job claim one of the system's lease types (R63) — set
    together by :meth:`box_service_queue.JobQueue.claim`/``heartbeat`` and cleared
    on release, never assigned individually.

    ``queued_at`` is stamped once, at creation, and never touched again — it is
    the fixed reference point ``box_service_observability.find_stuck_jobs`` (R3)
    measures a still-``queued`` job's age against.

    ``terminal_at`` is stamped only by :func:`mark_terminal`'s callers (R24) —
    from a discrete terminal event's own timestamp, never a wall-clock read
    taken when that event happens to be processed — so the recorded terminal
    moment is never inferred from a poll interval.

    ``tail_ref`` is an opaque key into the object store
    ``box_service_activity_tail.ActivityTailStore`` holds the job's bounded
    rolling activity tail under (R25) — the job row carries only this
    reference, never the tail content itself, so no tail content is ever
    stored as a Praxis fact.
    """

    id: str
    project: str
    snapshot: str
    state: JobState
    session_id: str | None = None
    run_owner: str | None = None
    attempt_count: int = 0
    max_attempts: int = 3
    resumable: bool = False
    failure_reason: str | None = None
    group_id: str | None = None
    org: str = "default"
    claim_lease: Lease | None = None
    worktree_path: str | None = None
    claim_heartbeat_at: float | None = None
    claim_lease_ttl: float | None = None
    queued_at: float | None = None
    last_activity_at: float | None = None
    """Last-activity timestamp maintained from harness-fired hook events
    alone (R22, see ``knowledge/serve/box_service_activity.py``) -- the
    external session poll (:class:`SessionInfo`) carries a start time but no
    activity time. ``None`` until the first hook event fires."""
    terminal_at: float | None = None
    tail_ref: str | None = None

    def is_open(self) -> bool:
        return self.state in OPEN_JOB_STATES


@dataclass(frozen=True)
class SessionInfo:
    """One row of ``claude agents --json`` (R21) — session existence and
    state as externally observed, independent of the build session's
    cooperation (R20)."""

    session_id: str
    cwd: str
    kind: str
    started_at: str
    name: str
    state: str
