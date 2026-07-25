"""GET /productivity: the four bucketed series (S1-S4), owner-gated (R3).

Registered as a nested ``@app.get`` inside ``create_app`` next to
``/requirements/completeness`` (same ``current_user``/``active_org``/``active_user_id``
dependencies — see ``app.py``). This module holds the parts that don't need the FastAPI
app object itself: the range -> bucket-boundary plan, the token-owner allowlist check, and
the series assembly, so the route handler stays a thin wire-up the same way every other
route in ``app.py`` is.

The owner gate is enforced in the SAME change as the route (R5, the peer-owner-gate
ticket, was rejected as subsumed into this one) so S1-S3 — derived from the single
backend-held GitHub token — are never reachable by any principal but the token's owner,
including one authenticated by an org API key (whose ``Principal.email`` is always
``None`` and therefore never matches the owner allowlist below).

Only ``range`` is ever read from the request: no client-supplied UTC-offset or IANA zone
name query parameter is accepted anywhere in this module, so bucket boundaries can never
vary per caller (a client-supplied zone would both reopen the DST bucket-boundary bug and
turn "the caller" into an unbounded cache-key dimension — see the
``no-client-supplied-timezone`` build check).
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from knowledge.serve import github_audit, github_commits, github_token, productivity_cache
from knowledge.serve.auth import Principal
from knowledge.serve.productivity_attribution import bucketed_owner_totals, net_lines
from knowledge.serve.productivity_series import s4_series

# The one range values the route accepts (R8: each range selects the bucket_unit and bucket
# count below — see bucket_plan).
DAY = "day"
WEEK = "week"
FOUR_WEEKS = "4weeks"
TWELVE_MONTHS = "12months"
ALLTIME = "alltime"
ALLOWED_RANGES = {DAY, WEEK, FOUR_WEEKS, TWELVE_MONTHS, ALLTIME}

_HOUR = 3600.0
_DAY = 86400.0
_WEEK = 7 * _DAY

DEFAULT_OWNER_LOGIN = "mattdaw7"
DEFAULT_OWNER_EMAIL = "mattdaw7@gmail.com"


def kill_switch_enabled() -> bool:
    """True iff the productivity feature is administratively disabled (R39).

    Read fresh on every call (no caching) so flipping ``PRODUCTIVITY_KILL_SWITCH``
    takes effect on the very next request, no redeploy required: the route degrades
    to a disabled status before the owner gate and before any GitHub call, so a
    leaked or revoked token is contained immediately regardless of who is asking.
    """
    return os.environ.get("PRODUCTIVITY_KILL_SWITCH", "").strip().lower() in {
        "1", "true", "yes",
    }


def owner_login() -> str:
    """The GitHub login whose commits count toward S1/S2 (see ``productivity_attribution``)."""
    return os.environ.get("PRODUCTIVITY_OWNER_LOGIN", "").strip() or DEFAULT_OWNER_LOGIN


def owner_emails() -> list[str]:
    """The Praxis-principal email(s) allowed past the token-owner gate.

    ``PRODUCTIVITY_OWNER_EMAIL`` is the primary (defaults to the named token owner from
    ``docs/solutions/conventions/github-token-storage.md``); ``PRODUCTIVITY_OWNER_EMAILS``
    is an optional comma-separated list of additional verified addresses for the same
    person (mirrors the login/email fallback ``attribute_commit_activity`` already uses).
    """
    primary = os.environ.get("PRODUCTIVITY_OWNER_EMAIL", "").strip() or DEFAULT_OWNER_EMAIL
    extra = [
        e.strip()
        for e in os.environ.get("PRODUCTIVITY_OWNER_EMAILS", "").split(",")
        if e.strip()
    ]
    return [primary, *extra]


def is_owner(principal: Principal) -> bool:
    """True iff ``principal`` is the identity the backend's GitHub token belongs to.

    An explicit allowlist check on top of ``current_user``/``active_org`` (R5's rejected
    acceptance condition, carried verbatim into R3): a principal with no verified email —
    every API-key principal, per ``Principal.api_key_org`` — can never match and is always
    refused, never silently passed through.
    """
    email = (principal.email or "").strip().lower()
    if not email:
        return False
    return email in {e.lower() for e in owner_emails()}


def bucket_plan(range_: str, now: datetime) -> tuple[list[datetime], float, date, date, str]:
    """Return ``(bucket_starts, bucket_seconds, window_start, window_end, bucket_unit)`` for
    ``range_`` (R8).

    Each range selects the bucket width so every selectable range yields enough points to
    draw a line: hourly for ``day``, daily for ``week``/``4weeks``, weekly for ``12months``,
    monthly for ``alltime``. ``bucket_unit`` names that width (``"hour"``/``"day"``/``"week"``/
    ``"month"``) so the response can disclose it and the client can label the axis in matching
    units. Buckets are contiguous, equal-width, half-open windows ending at ``now`` (the most
    recent bucket is the one ``now`` currently falls in); each bucket SUMS the values of the
    periods it contains (never averages) — a property of how callers accumulate into these
    windows, not of the plan itself. ``window_start``/``window_end`` are the calendar-date
    bounds the GitHub commit-activity fetch spans.
    """
    if range_ not in ALLOWED_RANGES:
        raise ValueError(f"unknown range {range_!r}")

    if range_ == DAY:
        count, seconds, unit = 24, _HOUR, "hour"
    elif range_ == WEEK:
        count, seconds, unit = 7, _DAY, "day"
    elif range_ == FOUR_WEEKS:
        count, seconds, unit = 28, _DAY, "day"
    elif range_ == TWELVE_MONTHS:
        count, seconds, unit = 52, _WEEK, "week"
    else:  # ALLTIME
        count, seconds, unit = 60, 30 * _DAY, "month"  # 5yr lookback (same floor as R9).

    span = timedelta(seconds=seconds * count)
    first_start = now - span
    bucket_starts = [first_start + timedelta(seconds=seconds * i) for i in range(count)]
    return bucket_starts, seconds, first_start.date(), now.date(), unit


def _series_points(bucket_starts: list[datetime], values: list[int]) -> list[dict[str, Any]]:
    return [
        {"bucket_start": start.isoformat(), "value": value}
        for start, value in zip(bucket_starts, values)
    ]


def build_series(conn: Any, org_id: str, range_: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Assemble the four named series for ``range_``, scoped to ``org_id``.

    Fetches the owner's GitHub commit activity for the resolved window (S1 additions, S2
    deletions, S3 their difference) and the org-wide finished-ticket counts (S4, R7),
    bucketed identically so every series lines up on the same ``bucket_start`` axis.
    """
    started_at = time.perf_counter()
    now = now or datetime.now(timezone.utc)
    bucket_starts, bucket_seconds, window_start, window_end, bucket_unit = bucket_plan(range_, now)

    token = github_token.resolve_github_token()
    activity = github_commits.fetch_commit_activity(
        owner_login(), window_start, window_end, token=token
    )
    github_audit.record_github_use("/productivity", len(activity.get("repositories") or {}))

    totals = bucketed_owner_totals(
        activity.get("repositories") or {},
        owner_login(),
        bucket_starts,
        bucket_seconds,
        owner_emails=owner_emails(),
    )
    s1, s2 = totals["s1"], totals["s2"]
    s3 = net_lines(s1, s2)
    s4 = s4_series(conn, org_id, bucket_starts, bucket_seconds)

    github_audit.record_productivity_request(
        duration_ms=(time.perf_counter() - started_at) * 1000,
        points_spent=int(activity.get("points_spent") or 0),
        cache_hit=False,
        truncated=bool(activity.get("truncated")),
    )

    return {
        "range": range_,
        "bucket_unit": bucket_unit,
        "truncated": bool(activity.get("truncated")),
        "series": {
            "s1_lines_added": _series_points(bucket_starts, s1),
            "s2_lines_deleted": _series_points(bucket_starts, s2),
            "s3_net_lines": _series_points(bucket_starts, s3),
            "s4_tickets_completed": _series_points(bucket_starts, s4),
        },
    }


def get_series_cached(
    conn: Any, org_id: str, user_key: str, range_: str, *, now: datetime | None = None,
) -> dict[str, Any]:
    """Serve ``/productivity`` from the short/long-TTL cache when possible (R4).

    Cached on ``(org_id, user_key, range_)`` — the caller's org/identity and the
    requested window, never a client-supplied timezone (see module docstring). A
    hit returns the EXACT prior payload — same ``computed_at`` — and never calls
    GitHub or Praxis again; a miss calls :func:`build_series`, stamps
    ``computed_at`` onto the result, and caches it for that range's TTL band (D7).
    Either path logs one observability record (R40): the miss path logs from
    inside :func:`build_series`, the hit path logs here (zero GitHub points spent).

    Concurrent misses for the SAME cache key are coalesced (single-flight): only
    the first caller to acquire this key's lock computes and calls GitHub; every
    other simultaneous caller blocks on the same lock and, once it clears, finds
    the winner's payload already cached and returns THAT exact object — same
    ``computed_at`` — instead of racing its own redundant GitHub fan-out.

    STALE FALLBACK (R22): when the live compute comes back truncated with a
    reason (a GitHub timeout, upstream error, or rate limit that survived R37's
    retries), and a previously-computed payload exists for this key (regardless
    of its TTL — see :func:`productivity_cache.get_stale`), that cached payload is
    served instead, marked ``stale: True`` (and ``rate_limited: True`` iff the
    failure was specifically a rate limit) with its ORIGINAL ``computed_at``
    intact — never presented as freshly computed. Only when there is no prior
    payload to fall back on is the truncated fresh result returned as-is.
    """
    now = now or datetime.now(timezone.utc)
    ts = now.timestamp()
    cached = productivity_cache.get(org_id, user_key, range_, now=ts)
    if cached is not None:
        github_audit.record_productivity_request(
            duration_ms=0.0,
            points_spent=0,
            cache_hit=True,
            truncated=bool(cached.get("truncated")),
        )
        return cached

    lock = productivity_cache.lock_for(org_id, user_key, range_)
    with lock:
        # Re-check: another thread may have populated the cache while we were
        # waiting for the lock -- that thread's payload wins, we never recompute.
        cached = productivity_cache.get(org_id, user_key, range_, now=ts)
        if cached is not None:
            github_audit.record_productivity_request(
                duration_ms=0.0,
                points_spent=0,
                cache_hit=True,
                truncated=bool(cached.get("truncated")),
            )
            return cached

        result = build_series(conn, org_id, range_, now=now)
        if result.get("reason") is not None:
            stale = productivity_cache.get_stale(org_id, user_key, range_)
            if stale is not None:
                stale_response = dict(stale)
                stale_response["stale"] = True
                stale_response["rate_limited"] = (
                    result.get("reason") == github_commits.TruncationReason.RATE_LIMITED
                )
                return stale_response

        result["computed_at"] = now.isoformat()
        result.setdefault("stale", False)
        result.setdefault("rate_limited", False)
        productivity_cache.put(org_id, user_key, range_, result, now=ts)
        return result
