"""In-memory job store (R1): a job is a first-class, queryable entity with a
stable id, distinct from the tickets it builds. Real persistence (Postgres
backing, atomic claim compare-and-set) is separate later work; this module
owns only the lifecycle contract R1 itself requires, so it is unit-testable
without a database: create + query by id, and the ``awaiting-human`` ->
``running`` transition, which mutates the SAME job row rather than creating a
new one ("awaiting-human is a mid-run state the job returns from ... without
a new job being created").
"""

from __future__ import annotations

import uuid

from knowledge.serve.box_service_models import Job, JobState


class JobStore:
    """A keyed-by-id collection of job rows."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def create(self, *, project: str, snapshot: str) -> Job:
        """Create and store a new job, queued, with a fresh stable id."""
        job = Job(id=str(uuid.uuid4()), project=project, snapshot=snapshot, state=JobState.QUEUED)
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        """Query a job row by id, or ``None`` if no such job exists."""
        return self._jobs.get(job_id)

    def resume_from_awaiting_human(self, job_id: str) -> Job:
        """Transition the job back to ``running`` in place. Raises if the job
        is not currently ``awaiting-human`` — resuming is a transition on an
        existing row, never a way to create one.
        """
        job = self._jobs[job_id]
        if job.state is not JobState.AWAITING_HUMAN:
            raise ValueError(
                f"job {job_id!r} is {job.state.value!r}, not awaiting-human; cannot resume"
            )
        job.state = JobState.RUNNING
        return job

    def enter_awaiting_human(self, job_id: str, reason: str) -> Job:
        """Transition a mid-run job into ``awaiting-human`` (R23), in place —
        mutating the SAME row, never creating a new one, so
        :meth:`resume_from_awaiting_human` later returns it under the
        identical job id. Raises if the job is already terminal (a job that
        is done cannot newly need a human) or ``reason`` is empty (the
        awaiting-human pause must say what question it is waiting on).
        """
        job = self._jobs[job_id]
        if job.is_terminal():
            raise ValueError(
                f"job {job_id!r} is terminal ({job.state.value!r}); cannot enter awaiting-human"
            )
        if not reason:
            raise ValueError("awaiting-human requires a non-empty reason")
        job.state = JobState.AWAITING_HUMAN
        job.reason = reason
        return job
