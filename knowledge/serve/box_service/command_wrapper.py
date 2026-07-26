"""The command-wrapper seam (R83).

Steps that contend on a non-file resource (a fixed test-fixture port, a
shared database) must be serialized by a host-level advisory lock wrapping
the contending command itself — but that lock is asserted against a NAMED
injectable seam, ``CommandWrapper``, rather than a real host lock, so the
unit contract never depends on actually holding an OS-level lock.
"""

from __future__ import annotations

import fcntl
import subprocess
from pathlib import Path
from typing import Callable, Protocol


class _RunResult(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., _RunResult]


def _default_runner(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


class Lock(Protocol):
    """The injectable lock half of the seam: acquire blocks until the named
    key is exclusively held; release always frees it."""

    def acquire(self, key: str) -> None: ...

    def release(self, key: str) -> None: ...


class FileLock:
    """Real lock: one ``flock``-held file per key under a lock directory, so
    concurrent host processes (not just threads in one process) serialize on
    the same key."""

    def __init__(self, *, lock_dir: str = "/tmp/box-service-locks") -> None:
        self._lock_dir = Path(lock_dir)
        self._handles: dict[str, object] = {}

    def acquire(self, key: str) -> None:
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        path = self._lock_dir / key.replace("/", "_")
        handle = open(path, "w")
        fcntl.flock(handle, fcntl.LOCK_EX)
        self._handles[key] = handle

    def release(self, key: str) -> None:
        handle = self._handles.pop(key, None)
        if handle is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()


class CommandWrapper:
    """The named seam a lock-wrapped invocation is asserted against: acquire
    the advisory lock for ``lock_key``, run the command, always release —
    even when the command fails."""

    def __init__(self, *, lock: Lock, runner: Runner = _default_runner) -> None:
        self._lock = lock
        self._runner = runner

    def run(self, argv: list[str], *, lock_key: str, **kwargs) -> _RunResult:
        self._lock.acquire(lock_key)
        try:
            return self._runner(argv, **kwargs)
        finally:
            self._lock.release(lock_key)


# Kept as the name the seam's own unit tests import — an alias makes the
# real-lock default explicit at the call site without a second class.
AdvisoryLockCommandWrapper = CommandWrapper
