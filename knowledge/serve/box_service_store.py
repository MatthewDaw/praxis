"""In-memory job store (R1): a job is a first-class, queryable entity with a
stable id, distinct from the tickets it builds. Real persistence (Praxis /
Postgres backing, atomic claim compare-and-set) is separate later work
(R2/R3); this module owns only the lifecycle contract R1 itself requires so
it is unit-testable without a database: create + query by id, and the
awaiting-human -> running transition, which mutates the SAME job row rather
than creating a new one ("awaiting-human is a mid-run state the job returns
from ... without a new job being created").
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from knowledge.serve.box_service_models import Job, JobState, TERMINAL_JOB_STATES


class JobStore:
    """A keyed-by-id collection of job rows.

    ``clock`` is injectable so ``queued_at`` (R3) can be asserted
    deterministically in tests without sleeping past a real threshold.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._jobs: dict[str, Job] = {}

    def create(self, *, project: str, snapshot: str) -> Job:
        """Create and store a new job, queued, with a fresh stable id. Stamps
        ``queued_at`` (R3) once at creation, the fixed reference point a still-
        queued job's age is later measured against.
        """
        job = Job(
            id=str(uuid.uuid4()),
            project=project,
            snapshot=snapshot,
            state=JobState.QUEUED,
            queued_at=self._clock(),
        )
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        """Query a job row by id, or ``None`` if no such job exists."""
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        """Every stored job row (R26: the "which jobs are live" listing query),
        in creation order."""
        return list(self._jobs.values())

    def enter_awaiting_human(self, job_id: str, question: str) -> Job:
        """Transition a running job into ``awaiting-human`` with a question.

        The question is persisted as its own field on the job (distinct from
        ``failure_reason``) so the jobs view can render it next to the reply
        control (R79). Raises ``ValueError`` if the question is empty or the
        job is already in a terminal state.
        """
        if not question or not question.strip():
            raise ValueError("question must not be empty")
        job = self._jobs[job_id]
        if job.state in TERMINAL_JOB_STATES:
            raise ValueError(
                f"job {job_id!r} is {job.state.value!r} (terminal); cannot enter awaiting-human"
            )
        job.state = JobState.AWAITING_HUMAN
        job.question = question.strip()
        return job

    def resume_from_awaiting_human(self, job_id: str) -> Job:
        """Transition the job back to ``running`` in place. Raises if the job
        is not currently ``awaiting-human`` — resuming is a transition on an
        existing row, never a way to create one. Clears ``question`` (R79):
        the pause it named is now resolved, so a later awaiting-human pause
        on the same job never reads a stale prior question.
        """
        job = self._jobs[job_id]
        if job.state is not JobState.AWAITING_HUMAN:
            raise ValueError(
                f"job {job_id!r} is {job.state.value!r}, not awaiting-human; cannot resume"
            )
        job.state = JobState.RUNNING
        job.question = None
        return job
