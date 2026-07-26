"""The job claim lease (R2): claiming a queued job is an atomic
compare-and-set that establishes a heartbeated lease, and a claimed job whose
lease goes stale returns to ``queued`` for another claim.

Mirrors the existing requirement-ticket lease pattern
(``knowledge/knowledge_graph/knowledge_graph_variants/postgres_vector_graph.py``'s
``claim_requirement``/``heartbeat_requirement``) at the same granularity this
package's job model uses elsewhere (R1, R43, R68): a holder id
(``Job.run_owner``), a heartbeat (``Job.claim_heartbeat_at``), and an expiry
(``Job.claim_lease_ttl``) — the three fields every lease type in the system
must carry (R63) — so a dead claimant never permanently strands a queued job.

``JobQueue`` is the single writer of these three fields; it holds an
in-process lock around the whole decide-and-mutate step so two concurrent
callers racing the same job id can never both observe it as claimable (the
loser gets :class:`LeaseConflict`, never a second silent grant).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from knowledge.serve.box_service_models import Job, JobState

#: Default lease length (seconds) a claim grants before it goes stale.
DEFAULT_CLAIM_LEASE_TTL_SECONDS = 900.0


class LeaseConflict(RuntimeError):
    """Raised when a claim/heartbeat is refused because a different owner
    holds a still-live lease on the job."""

    def __init__(self, *, owner: str | None, remaining: float) -> None:
        self.owner = owner
        self.remaining = remaining
        super().__init__(f"job held by {owner!r} for {remaining:.1f}s more")


def _lease_live(job: Job, now: float) -> bool:
    """True iff ``job`` is claimed/running under a non-stale heartbeat."""
    if job.run_owner is None or job.claim_heartbeat_at is None or job.claim_lease_ttl is None:
        return False
    if job.state not in (JobState.CLAIMED, JobState.RUNNING):
        return False
    return (now - job.claim_heartbeat_at) <= job.claim_lease_ttl


class JobQueue:
    """An in-process job store keyed by job id, owning the claim lease.

    ``clock`` is injectable so lease-expiry can be asserted deterministically
    in tests without sleeping past a real TTL.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def enqueue(self, job: Job) -> Job:
        """Add ``job`` to the queue (expected ``state=JobState.QUEUED``)."""
        with self._lock:
            self._jobs[job.id] = job
            return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return job

    def claim(
        self, job_id: str, owner: str, ttl: float = DEFAULT_CLAIM_LEASE_TTL_SECONDS
    ) -> Job:
        """Atomically grant ``owner`` the claim lease on ``job_id``.

        Grants iff the job is not held by a different LIVE lease — i.e. it is
        ``queued``, OR ``owner`` already holds it (idempotent renew), OR the
        existing lease is stale (heartbeat older than its TTL). Two concurrent
        claims for the same queued job yield exactly one grant: whichever
        caller acquires the lock first transitions the job to ``claimed`` and
        stamps the lease, so the second caller's own re-check inside the same
        lock sees a live lease under a different owner and raises
        :class:`LeaseConflict` — never a second silent grant.

        Raises :class:`KeyError` for an unknown job id, :class:`LeaseConflict`
        if a different owner holds a live lease.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            now = self._clock()
            live = _lease_live(job, now)
            if live and job.run_owner != owner:
                remaining = job.claim_lease_ttl - (now - job.claim_heartbeat_at)  # type: ignore[operator]
                raise LeaseConflict(owner=job.run_owner, remaining=remaining)
            job.state = JobState.CLAIMED
            job.run_owner = owner
            job.claim_heartbeat_at = now
            job.claim_lease_ttl = ttl
            return job

    def heartbeat(self, job_id: str, owner: str) -> Job:
        """Renew ``owner``'s live lease on ``job_id`` (bump the heartbeat).

        Raises :class:`LeaseConflict` if ``owner`` no longer holds a live
        lease (lost to staleness or to another claimant) and :class:`KeyError`
        for an unknown job id.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            now = self._clock()
            if job.run_owner != owner or not _lease_live(job, now):
                raise LeaseConflict(owner=job.run_owner, remaining=0.0)
            job.claim_heartbeat_at = now
            return job
