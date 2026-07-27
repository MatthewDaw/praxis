"""Per-repo host advisory lock (R18).

Commands that contend on a fixed host port or a shared test fixture (the
local Postgres container, e.g.) are serialized by a host-level advisory
lock wrapping the contending command itself, keyed per repo and taken per
invocation. The lock's scope is deliberately narrow: only commands
``is_contending_command`` classifies as fixture/fixed-host-port commands
ever acquire it (via :func:`run_locked`) -- a broader lock wrapping build
commands generally would serialize concurrent jobs that do not actually
contend on anything.

Lease semantics: each acquisition is a lease with a holder id, a heartbeat
and an expiry (the cross-cutting invariant every lease in this system
carries -- job claim, job-control, and this lock alike), stamped into the
lock file. The OS-level ``flock`` itself is what makes a dead holder's
lease reclaimable: the kernel releases it the instant the holding process
exits, so no separate reap step is needed.

``run_locked`` is also the seam ``box_service_deploy_guard.guard_command``
runs in front of: a deploy-class command is refused outright before this
module ever classifies it, so the lock's scope stays limited to the
fixture/fixed-host-port class ``is_contending_command`` recognizes.
"""

from __future__ import annotations

import fcntl
import os
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, TypeVar

from knowledge.serve.box_service_deploy_guard import guard_command

DEFAULT_LOCK_DIR = "/tmp/box-service-host-locks"
DEFAULT_LEASE_TTL_S = 900

#: Commands classified as contending on a fixed host port or a shared test
#: fixture -- the only class of command the host advisory lock wraps.
_CONTENDING_MARKERS = (
    "docker compose",
    "docker-compose",
    "local-db.sh",
    "db-up",
    "db-bootstrap",
    "db-down",
    "knowledge/serve/tests",
)

T = TypeVar("T")


@dataclass(frozen=True)
class HostLockLease:
    """The lease metadata stamped into a held lock: holder id, heartbeat,
    and expiry -- present for observability even though the OS-level flock
    is what actually enforces reclaim-on-death."""

    holder: str
    heartbeat_at: float
    expires_at: float


def is_contending_command(command: str) -> bool:
    """True iff ``command`` contends on a fixed host port or a shared test
    fixture, i.e. the narrow class this lock exists to serialize."""
    return any(marker in command for marker in _CONTENDING_MARKERS)


class HostAdvisoryLock:
    """A per-repo advisory lock, held only while a contending command runs.

    Keyed per repo (``repo_key``) so two jobs against different repos never
    contend with each other, and taken per invocation (acquired fresh for
    each contending command rather than held across a job's lifetime).
    """

    def __init__(self, lock_dir: str | Path = DEFAULT_LOCK_DIR, ttl: int = DEFAULT_LEASE_TTL_S) -> None:
        self._lock_dir = Path(lock_dir)
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        self._ttl = ttl

    def _lock_path(self, repo_key: str) -> Path:
        safe = repo_key.replace("/", "_").replace(os.sep, "_")
        return self._lock_dir / f"{safe}.lock"

    @contextmanager
    def acquire(self, repo_key: str) -> Iterator[HostLockLease]:
        """Block until the per-repo lock is free, then hold it for the
        duration of the ``with`` block. Released (and the lease implicitly
        expired) on exit -- including if the holding process dies, since the
        OS releases an ``flock`` when its owning file descriptor closes."""
        path = self._lock_path(repo_key)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            now = time.time()
            lease = HostLockLease(holder=uuid.uuid4().hex, heartbeat_at=now, expires_at=now + self._ttl)
            os.ftruncate(fd, 0)
            os.write(fd, f"{lease.holder}:{lease.heartbeat_at}:{lease.expires_at}".encode())
            os.fsync(fd)
            yield lease
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def run_locked(
    repo_key: str,
    command: str,
    runner: Callable[[], T],
    *,
    lock: HostAdvisoryLock | None = None,
) -> T:
    """Run ``command`` via ``runner`` (a zero-arg callable), taking the
    per-repo host advisory lock only if ``command`` is a contending command.
    Non-contending commands run unlocked so concurrent non-contending jobs
    never wait on each other.

    A deploy-class command is refused (``DeployCommandRefused``) before this
    function ever classifies or runs it -- see ``box_service_deploy_guard``."""
    guard_command(command)
    if is_contending_command(command):
        lock = lock or HostAdvisoryLock()
        with lock.acquire(repo_key):
            return runner()
    return runner()
