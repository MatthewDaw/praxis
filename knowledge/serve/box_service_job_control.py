"""The job-control lease (R30): resume and reap are mutually exclusive per job through a single
serialized job-control path.

A liveness check alone (poll the daemon, decide, act) is read-then-act against a background
process — the read is stale by construction the instant an operator resumes between the read and
the act. So resume and the backstop reaper are serialized instead through one shared lease on the
job row: :func:`take_control_lease` is called BEFORE ``resume_job`` launches a new session, and the
reaper (``box_service_reaper.reap_terminal_session``) refuses to act on any job that currently holds
a live lease. Whichever side takes the lease first wins the race; the loser (the reaper, since it
only ever reads the lease, never contends for it) simply does nothing this pass — a pending reap is
"cancelled" by never having anything left to act on, not by an explicit cancel call.

Reuses :class:`box_service_models.Lease` (holder id + heartbeat + expiry, R63) rather than inventing
a second lease shape — the job-control lease is one more instance of the one lease pattern every
lease type in the system shares (R2's claim lease, the host advisory lock).
"""

from __future__ import annotations

import time

from knowledge.serve.box_service_models import Job, Lease

#: How long a job-control lease is honored before it is treated as abandoned. Short-lived on
#: purpose: it only needs to outlive the resume-vs-reap race window, not a whole build run (unlike
#: the job claim lease's much longer TTL).
CONTROL_LEASE_TTL_S = 60.0


def take_control_lease(job: Job, holder_id: str, *, now: float | None = None,
                        ttl: float = CONTROL_LEASE_TTL_S) -> Job:
    """Grant ``holder_id`` the job-control lease on ``job``, unconditionally overwriting any prior
    lease. Resume is the only caller (see ``box_service_resume.resume_job``) and always wins the
    race against the reaper, which never contends for the lease — it only ever reads it."""
    now = time.time() if now is None else now
    job.control_lease = Lease(holder_id=holder_id, heartbeat_at=now, expires_at=now + ttl)
    return job


def control_lease_is_live(job: Job, *, now: float | None = None) -> bool:
    """True iff ``job`` currently holds a job-control lease that has not yet expired — the signal
    the reaper checks before acting on a job (a dead holder's lease simply expires, so it never
    permanently strands the job for the reaper, R63)."""
    now = time.time() if now is None else now
    lease = job.control_lease
    return lease is not None and lease.is_live(now)
