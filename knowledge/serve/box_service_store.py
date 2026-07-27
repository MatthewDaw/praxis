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
        """Every stored job row (R26's ``GET /jobs`` listing reads this)."""
        return list(self._jobs.values())

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

    def enter_awaiting_human(self, job_id: str, question: str) -> Job:
        """Transition a mid-run job into ``awaiting-human`` on a blocked-on-
        question event (R23/R79), mutating the SAME row in place so
        :meth:`resume_from_awaiting_human` later returns it under the
        identical job id. ``question`` is persisted as its own queryable
        field (R79) distinct from ``failure_reason`` -- the job view reads
        it directly, and it is never folded into the terminal-failure
        vocabulary. Raises if the job is already terminal, or if
        ``question`` is empty (an awaiting-human pause must say what
        question it is waiting on).
        """
        job = self._jobs[job_id]
        if job.state in TERMINAL_JOB_STATES:
            raise ValueError(
                f"job {job_id!r} is terminal ({job.state.value!r}); cannot enter awaiting-human"
            )
        if not question:
            raise ValueError("awaiting-human requires a non-empty question")
        job.state = JobState.AWAITING_HUMAN
        job.question = question
        return job
