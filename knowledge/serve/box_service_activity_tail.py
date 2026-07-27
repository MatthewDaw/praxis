"""Bounded rolling activity tail (R25): recent job activity is stored in an
object store keyed off the job row's ``tail_ref`` — never as the content of a
Praxis fact — so recent messages stay readable after the session is reaped,
the box is unreachable, or the process died. A deeper live fetch is
available only while the session still exists (AE8).

This module owns the pure store logic (append/read/purge/cascade-delete); it
holds no Praxis or subprocess dependency, matching every other
``box_service_*`` building block (see ``box_service_store.py``). Real
persistence (S3/blob backing) is later infrastructure work — the same
"in-memory now, real backing later" split the job store itself already took.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from knowledge.serve.box_service_models import Job
from knowledge.serve.job_authz import JobAction, JobPrincipal, JobRef, authorize

#: Default bounded size (bytes) of a job's rolling tail (R25's "bounded").
DEFAULT_TAIL_BYTE_CAP = 8_000

#: Default retention window (seconds) past which a stored tail is purged
#: (R66: 90 days for observation events).
DEFAULT_RETENTION_SECONDS = 90 * 24 * 3600.0


@dataclass
class _TailEntry:
    project: str
    content: bytes
    updated_at: float


class ActivityTailStore:
    """An object store for bounded rolling activity tails, addressed by the
    opaque ref stamped on ``Job.tail_ref``.

    ``clock`` is injectable so byte-cap rotation and retention purging are
    assertable deterministically in tests without sleeping past a real
    window (the same pattern ``JobStore``/``JobQueue`` use).
    """

    def __init__(
        self,
        *,
        byte_cap: int = DEFAULT_TAIL_BYTE_CAP,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._byte_cap = byte_cap
        self._clock = clock
        self._entries: dict[str, _TailEntry] = {}

    def append(self, job: Job, chunk: str) -> str:
        """Append ``chunk`` to ``job``'s rolling tail, rotating out the
        oldest bytes once the bounded cap is exceeded. Stamps (or reuses)
        ``job.tail_ref`` and returns it — the job row carries only this
        reference, never the tail content itself.
        """
        ref = job.tail_ref or f"tail:{job.id}"
        job.tail_ref = ref
        existing = self._entries.get(ref)
        content = (existing.content if existing else b"") + chunk.encode("utf-8")
        if len(content) > self._byte_cap:
            content = content[-self._byte_cap :]  # rotate out the oldest bytes
        self._entries[ref] = _TailEntry(
            project=job.project, content=content, updated_at=self._clock()
        )
        return ref

    def has_entry(self, ref: str | None) -> bool:
        """Whether ``ref`` currently has a persisted tail entry. The query
        surface ``box_service_worktree_cleanup`` reads to confirm a job's
        tail has actually persisted (R40), rather than reaching into this
        store's private state."""
        return ref is not None and ref in self._entries

    def read_stored(self, job: Job, principal: JobPrincipal | None) -> str:
        """Return the stored bounded tail for ``job``, org-scope authorized
        (R52's ``job_authz``) — the path a reaped session's history, an
        unreachable box, or a dead process's last activity is read through.
        Raises :class:`job_authz.AuthorizationError` for a cross-org or
        unauthenticated (``principal=None``) caller, regardless of whether
        the tail has been purged/never written.
        """
        job_ref = JobRef(
            id=job.id,
            org_id=job.org,
            owner_id=job.run_owner or "",
            lease_holder_id=job.run_owner,
        )
        authorize(JobAction.READ, principal, job_ref)
        entry = self._entries.get(job.tail_ref or "")
        if entry is None:
            return ""
        return entry.content.decode("utf-8", errors="replace")

    def read(
        self,
        job: Job,
        principal: JobPrincipal | None,
        *,
        session_alive: bool,
        live_fetch: Callable[[], str] | None = None,
    ) -> str:
        """The single read entrypoint (R25/AE8). While the session is alive,
        a deeper live fetch is used (expected to return MORE than the
        stored, capped tail); once the session is gone this falls back to
        the object store off ``job.tail_ref``. Authorization is enforced on
        every call, live or stored.
        """
        if session_alive and live_fetch is not None:
            self.read_stored(job, principal)  # authorization check, result discarded
            return live_fetch()
        return self.read_stored(job, principal)

    def purge_expired(self, *, retention_seconds: float = DEFAULT_RETENTION_SECONDS) -> int:
        """Drop every tail entry last updated more than ``retention_seconds``
        ago. Returns the count purged."""
        now = self._clock()
        expired = [
            ref for ref, entry in self._entries.items()
            if now - entry.updated_at > retention_seconds
        ]
        for ref in expired:
            del self._entries[ref]
        return len(expired)

    def delete_project(self, project: str) -> int:
        """Cascade-delete every tail entry belonging to ``project`` — a
        deleted project space takes its job history's stored activity tails
        with it. Returns the count deleted."""
        dead = [ref for ref, entry in self._entries.items() if entry.project == project]
        for ref in dead:
            del self._entries[ref]
        return len(dead)
