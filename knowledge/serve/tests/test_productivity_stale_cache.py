"""Stale-cache fallback for GET /productivity (R22).

Acceptance condition: given a response carrying a stale flag and a computed_at, the
panel displays the computed_at age and a stale marker adjacent to the chart. This
file covers the BACKEND half that produces such a response: when a live fetch fails
or is rate-limited, ``get_series_cached`` serves the last known good cached payload
(never evicted by TTL -- see ``productivity_cache.get_stale``) marked ``stale: True``
(and ``rate_limited: True`` iff the failure was a rate limit), preserving its
ORIGINAL ``computed_at`` rather than presenting the cached numbers as fresh.

Pure unit tests: ``build_series`` is monkeypatched directly so no DB/GitHub/Postgres
dependency is needed (mirrors the ``test_productivity_net_lines.py`` pure-unit style).
"""

from __future__ import annotations

from datetime import datetime, timezone

from knowledge.serve import productivity_cache, productivity_route
from knowledge.serve.github_commits import TruncationReason


def setup_function(_fn=None):
    productivity_cache.clear()


FRESH_SERIES = {
    "range": "week",
    "bucket_unit": "day",
    "truncated": False,
    "reason": None,
    "series": {
        "s1_lines_added": [{"bucket_start": "2026-07-18T00:00:00+00:00", "value": 120}],
        "s2_lines_deleted": [{"bucket_start": "2026-07-18T00:00:00+00:00", "value": 40}],
        "s3_net_lines": [{"bucket_start": "2026-07-18T00:00:00+00:00", "value": 80}],
        "s4_tickets_completed": [{"bucket_start": "2026-07-18T00:00:00+00:00", "value": 3}],
    },
}


def test_fresh_compute_is_cached_as_last_good_and_never_marked_stale(monkeypatch):
    monkeypatch.setattr(
        productivity_route, "build_series", lambda *a, **k: dict(FRESH_SERIES)
    )
    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    result = productivity_route.get_series_cached(None, "org-1", "user-1", "week", now=now)
    assert result["stale"] is False
    assert result["rate_limited"] is False
    assert result["computed_at"] == now.isoformat()


def test_rate_limited_failure_falls_back_to_last_good_stale_payload(monkeypatch):
    calls = {"n": 0}

    def fake_build_series(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return dict(FRESH_SERIES)
        return {**FRESH_SERIES, "truncated": True, "reason": TruncationReason.RATE_LIMITED}

    monkeypatch.setattr(productivity_route, "build_series", fake_build_series)

    first_now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    first = productivity_route.get_series_cached(None, "org-1", "user-1", "week", now=first_now)
    assert first["stale"] is False

    # Force the TTL-bound cache to have expired so the second call actually misses
    # and re-attempts a live fetch (which now fails) rather than serving the fresh hit.
    productivity_cache._store.clear()  # noqa: SLF001 (test seam, not the public API)

    second_now = datetime(2026, 7, 25, 13, 0, 0, tzinfo=timezone.utc)
    second = productivity_route.get_series_cached(None, "org-1", "user-1", "week", now=second_now)

    assert second["stale"] is True
    assert second["rate_limited"] is True
    # The ORIGINAL computed_at is preserved -- never presented as freshly computed.
    assert second["computed_at"] == first_now.isoformat()
    assert second["series"] == FRESH_SERIES["series"]


def test_timeout_failure_falls_back_stale_but_not_rate_limited(monkeypatch):
    calls = {"n": 0}

    def fake_build_series(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return dict(FRESH_SERIES)
        return {**FRESH_SERIES, "truncated": True, "reason": TruncationReason.TIMEOUT}

    monkeypatch.setattr(productivity_route, "build_series", fake_build_series)

    first_now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    productivity_route.get_series_cached(None, "org-1", "user-1", "week", now=first_now)
    productivity_cache._store.clear()  # noqa: SLF001

    second_now = datetime(2026, 7, 25, 13, 0, 0, tzinfo=timezone.utc)
    second = productivity_route.get_series_cached(None, "org-1", "user-1", "week", now=second_now)

    assert second["stale"] is True
    assert second["rate_limited"] is False


def test_failure_with_no_prior_cache_returns_the_truncated_result_unmarked_stale(monkeypatch):
    monkeypatch.setattr(
        productivity_route,
        "build_series",
        lambda *a, **k: {**FRESH_SERIES, "truncated": True, "reason": TruncationReason.RATE_LIMITED},
    )
    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    result = productivity_route.get_series_cached(None, "org-1", "user-1", "week", now=now)
    # No last-known-good entry exists yet, so there is nothing to fall back to.
    assert result["stale"] is False
    assert result["truncated"] is True


def test_get_stale_survives_ttl_eviction_of_the_fresh_store():
    productivity_cache.put("org-1", "user-1", "week", {"computed_at": "t0", "v": 1}, now=0.0)
    # Expire the TTL-bound entry directly...
    assert productivity_cache.get("org-1", "user-1", "week", now=10_000.0) is None
    # ...but the last-known-good entry is untouched.
    assert productivity_cache.get_stale("org-1", "user-1", "week") == {"computed_at": "t0", "v": 1}
