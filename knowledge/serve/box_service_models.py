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


@dataclass
class Job:
    """A box-service job row (R1, R31). ``session_id`` and ``run_owner`` are
    ``None`` until the job has been launched at least once. ``worktree_path``
    is the job's own worktree (R11) — the cwd its background session (R13)
    is launched into — and stays ``None`` until that worktree exists."""

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
    worktree_path: str | None = None

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
