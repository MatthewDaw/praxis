"""Stuck-job observability (R3): both queued-age and stale-claim-age are
queryable, so a job nothing picked up and a job whose claimant died are each
visible rather than silently stuck (see the brainstorm's "Box unreachable"
and "Stuck claim" failure classes).

Two distinct silences, one query:

- A job still ``queued`` past ``queued_threshold`` since :attr:`Job.queued_at`
  — nothing has claimed it (R3's "box unreachable" case).
- A ``claimed``/``running`` job whose claim lease (R2) has gone stale — its
  claimant died before its next heartbeat.

This module is pure decision logic — no Praxis, no I/O, no wall-clock reads —
so both cases are assertable deterministically from an injected ``now``. It
does not mutate or reclaim anything; :class:`box_service_queue.JobQueue`
already reclaims a stale-leased job on the next ``claim`` call. This is only
the read-side query that makes each kind of silence externally visible.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.box_service_queue import lease_is_live


class StuckReason(str, Enum):
    QUEUED_AGE = "queued-age"
    STALE_CLAIM_AGE = "stale-claim-age"


@dataclass(frozen=True)
class StuckJob:
    """One job flagged as stuck, with the reason and its age in seconds."""

    job: Job
    reason: StuckReason
    age: float


def find_stuck_jobs(
    jobs: Iterable[Job],
    *,
    now: float,
    queued_threshold: float,
) -> list[StuckJob]:
    """Return every job that is either queued longer than ``queued_threshold``
    or holds a stale claim lease, each with its respective age.

    A job can match at most one reason: it is either still ``queued`` (never
    claimed) or ``claimed``/``running`` (claimed, possibly with a dead
    claimant) — the two states this function distinguishes are mutually
    exclusive on ``Job.state``, so no job is double-reported.
    """
    stuck: list[StuckJob] = []
    for job in jobs:
        if job.state is JobState.QUEUED:
            if job.queued_at is None:
                continue
            age = now - job.queued_at
            if age > queued_threshold:
                stuck.append(StuckJob(job=job, reason=StuckReason.QUEUED_AGE, age=age))
        elif job.state in (JobState.CLAIMED, JobState.RUNNING):
            if job.claim_heartbeat_at is None or job.claim_lease_ttl is None:
                continue
            if not lease_is_live(job, now):  # the single shared definition of "stale" (R2)
                stuck.append(
                    StuckJob(
                        job=job,
                        reason=StuckReason.STALE_CLAIM_AGE,
                        age=now - job.claim_heartbeat_at,
                    )
                )
    return stuck
