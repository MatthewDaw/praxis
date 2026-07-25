"""In-process TTL cache for GET /productivity responses (R4).

Keyed by ``(org_id, user_key, range_)`` — never a client-supplied timezone: bucket
boundaries are fixed to America/Denver (D9) and the route accepts no client
UTC-offset or zone-name query parameter at all, so the zone can't become an
unbounded cache-key dimension (see the ``no-client-supplied-timezone`` build
check). TTL is short (60-120s, default 90s) for ranges of four weeks or less and
long (10-30min, default 20min) for the 12-month and all-time ranges (D7), each
tunable via env var.

Storage is a single in-process dict — the same single-App-Runner-instance
assumption ``rate_limit.py`` already documents for this deployment; if the
service ever scales horizontally this would need a shared backend (e.g. Redis).

Also holds the per-cache-key lock registry that ``productivity_route`` uses to
coalesce concurrent misses into a single upstream GitHub fan-out (single-flight):
see :func:`lock_for`.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

_SHORT_TTL_RANGES = {"day", "week", "4weeks"}
_LONG_TTL_RANGES = {"12months", "alltime"}

_DEFAULT_SHORT_TTL_SECONDS = 90.0
_DEFAULT_LONG_TTL_SECONDS = 20 * 60.0

_store: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}

_locks_guard = threading.Lock()
_key_locks: dict[tuple[str, str, str], threading.Lock] = {}


def lock_for(org_id: str, user_key: str, range_: str) -> threading.Lock:
    """The single-flight lock for this cache key, created lazily and reused.

    Two concurrent misses for the SAME ``(org_id, user_key, range_)`` must never
    both reach GitHub; concurrent misses for DIFFERENT keys must never block each
    other. A tiny meta-lock guards the registry dict itself (the window between
    "not present" and "insert" is the only race, and it's O(1) work).
    """
    key = (org_id, user_key, range_)
    with _locks_guard:
        lock = _key_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _key_locks[key] = lock
        return lock


def ttl_seconds(range_: str) -> float:
    """The cache TTL, in seconds, for ``range_`` — the D7 short/long band."""
    if range_ in _LONG_TTL_RANGES:
        return float(
            os.environ.get("PRODUCTIVITY_CACHE_LONG_TTL_SECONDS", _DEFAULT_LONG_TTL_SECONDS)
        )
    return float(
        os.environ.get("PRODUCTIVITY_CACHE_SHORT_TTL_SECONDS", _DEFAULT_SHORT_TTL_SECONDS)
    )


def get(org_id: str, user_key: str, range_: str, *, now: float | None = None) -> dict[str, Any] | None:
    """The cached payload for this key, or ``None`` if absent or expired (evicting it)."""
    now = time.time() if now is None else now
    key = (org_id, user_key, range_)
    entry = _store.get(key)
    if entry is None:
        return None
    expires_at, payload = entry
    if now >= expires_at:
        del _store[key]
        return None
    return payload


def put(
    org_id: str, user_key: str, range_: str, payload: dict[str, Any], *, now: float | None = None
) -> None:
    """Cache ``payload`` for this key until ``range_``'s TTL elapses."""
    now = time.time() if now is None else now
    key = (org_id, user_key, range_)
    _store[key] = (now + ttl_seconds(range_), dict(payload))


def clear() -> None:
    """Test seam: drop every cached entry and lock (module-global state persists across tests)."""
    _store.clear()
    with _locks_guard:
        _key_locks.clear()
