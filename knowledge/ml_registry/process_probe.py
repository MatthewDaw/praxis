"""Portable process liveness probing that survives PID reuse and zombies.

``os.kill(pid, 0)`` alone is not evidence that the process we launched is still
running: an unreaped child is a zombie that still answers signal 0, and a recycled
PID belongs to somebody else entirely.  Every probe here therefore pairs the PID
with the kernel's own start-time token for that PID.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time


PROC_STAT_AVAILABLE = Path("/proc/self/stat").exists()
PR_SET_PDEATHSIG = 1


def _proc_stat(pid: int) -> tuple[str, str] | None:
    """Return ``(state, start_token)`` from ``/proc/<pid>/stat`` on Linux."""
    if not PROC_STAT_AVAILABLE:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rindex(")") + 1:].split()
        return fields[0], fields[19]
    except (OSError, ValueError, IndexError):
        return None


def _ps_stat(pid: int) -> tuple[str, str] | None:
    """Return ``(state, start_token)`` from ``ps`` where ``/proc`` is absent."""
    try:
        completed = subprocess.run(
            ["ps", "-o", "state=,lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = completed.stdout.strip()
    if not line:
        return None
    state, _, started = line.partition(" ")
    return state, started.strip()


def probe(pid: object) -> tuple[bool, str | None]:
    """Return ``(alive, start_token)``; zombies and missing PIDs are not alive.

    ``start_token`` is ``None`` when the platform will not tell us, in which case
    callers must fall back to the conservative assumption that the PID is ours.
    """
    try:
        pid = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False, None
    if pid <= 0:
        return False, None
    stat = _proc_stat(pid)
    if stat is None:
        try:
            os.kill(pid, 0)
        except OSError:
            return False, None
        stat = _ps_stat(pid)
        if stat is None:
            return True, None
    state, started = stat
    return not state.startswith("Z"), started


def start_token(pid: object) -> str | None:
    return probe(pid)[1]


def matches(pid: object, expected: str | None) -> bool:
    """True when ``pid`` is alive and is the same process we recorded earlier."""
    alive, observed = probe(pid)
    if not alive:
        return False
    if expected is None or observed is None:
        return True
    return observed == expected


def terminate_group(pid: object, *, expected: str | None = None, grace: float = 5.0) -> bool:
    """SIGTERM the PID's process group, escalating to SIGKILL after ``grace``."""
    if not matches(pid, expected):
        return False
    try:
        target = int(pid)  # type: ignore[arg-type]
        group = os.getpgid(target)
    except (OSError, TypeError, ValueError):
        return False
    if group in {os.getpgrp(), 0}:
        return False
    try:
        os.killpg(group, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        if not probe(target)[0]:
            return True
        time.sleep(0.02)
    try:
        os.killpg(group, signal.SIGKILL)
    except OSError:
        pass
    return True


def set_parent_death_signal() -> None:
    """Ask Linux to SIGTERM this process when its parent dies; a no-op elsewhere."""
    if not PROC_STAT_AVAILABLE:
        return
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(
            PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0
        )
    except Exception:  # pragma: no cover - best effort hardening only
        pass
