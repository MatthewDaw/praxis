"""The box-service Job model (R1): a job is a first-class, queryable Praxis
entity with a stable id, distinct from the tickets it builds.

Its lifecycle is exactly seven named states — ``queued``, ``claimed``,
``running``, ``awaiting-human``, ``needs-attention``, ``completed`` and
``failed`` — and no others. ``awaiting-human`` is a mid-run state the job
returns from (back to ``running``) without a new job being created.
``needs-attention``, ``completed`` and ``failed`` are terminal: every one of
them carries a machine-readable ``reason`` field that is kept distinct from
the state value itself, so "why" a job ended up terminal is never conflated
with "what" state it is in.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class JobState(str, Enum):
    """The seven — and only seven — states a job's lifecycle may occupy."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting-human"
    NEEDS_ATTENTION = "needs-attention"
    COMPLETED = "completed"
    FAILED = "failed"


#: In-flight states: a job here is still doing (or about to resume doing) work.
#: ``awaiting-human`` counts as open — it is a mid-run pause, not an exit.
OPEN_JOB_STATES = frozenset(
    {JobState.QUEUED, JobState.CLAIMED, JobState.RUNNING, JobState.AWAITING_HUMAN}
)

#: At-rest states (R1). Every one of these MUST carry a machine-readable
#: reason (:attr:`Job.reason`) kept in a field distinct from the state value
#: itself — including ``COMPLETED``, so a completed job's "why" is recorded
#: the same disciplined way a failed one's is.
TERMINAL_JOB_STATES = frozenset(
    {JobState.COMPLETED, JobState.FAILED, JobState.NEEDS_ATTENTION}
)


@dataclass
class Job:
    """One job row: a stable id, its lifecycle state, and its terminal reason
    (when at rest). Distinct from the tickets it builds — a job's id is never
    a ticket/requirement id."""

    id: str
    project: str
    snapshot: str
    state: JobState = JobState.QUEUED
    reason: str | None = None
    worktree_path: str | None = None

    def is_open(self) -> bool:
        return self.state in OPEN_JOB_STATES

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_JOB_STATES


def mark_terminal(
    job: Job, state: JobState, reason: str, *, clear_worktree: bool = False
) -> Job:
    """Transition ``job`` to a terminal ``state`` with a machine-readable
    ``reason`` kept in a field distinct from the state value itself.

    ``clear_worktree`` defaults to False so a ``needs-attention`` job's
    worktree/branch artifacts are retained (for a human to inspect); a caller
    that has safely torn down the worktree after a clean completed/failed
    merge may pass ``clear_worktree=True``.
    """
    if state not in TERMINAL_JOB_STATES:
        raise ValueError(f"{state.value!r} is not a terminal JobState")
    if not reason:
        raise ValueError("a terminal job must carry a non-empty machine-readable reason")
    job.state = state
    job.reason = reason
    if clear_worktree:
        job.worktree_path = None
    return job
