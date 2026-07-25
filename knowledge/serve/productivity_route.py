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
from knowledge.serve.productivity_attribution import bucketed_owner_totals
from knowledge.serve.productivity_series import s4_series

# The one range values the route accepts. R8 (bucket-unit selection/aggregation) is a
# separate, not-yet-built ticket; the bucket widths chosen here are a reasonable
# equal-width placeholder pending that ticket, not a claim of calendar-exact buckets.
DAY = "day"
WEEK = "week"
FOUR_WEEKS = "4weeks"
TWELVE_MONTHS = "12months"
ALLTIME = "alltime"
ALLOWED_RANGES = {DAY, WEEK, FOUR_WEEKS, TWELVE_MONTHS, ALLTIME}

_HOUR = 3600.0
_DAY = 86400.0

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


def bucket_plan(range_: str, now: datetime) -> tuple[list[datetime], float, date, date]:
    """Return ``(bucket_starts, bucket_seconds, window_start, window_end)`` for ``range_``.

    Buckets are contiguous, equal-width, half-open windows ending at ``now`` (the most
    recent bucket is the one ``now`` currently falls in). ``window_start``/``window_end``
    are the calendar-date bounds the GitHub commit-activity fetch spans.
    """
    if range_ not in ALLOWED_RANGES:
        raise ValueError(f"unknown range {range_!r}")

    if range_ == DAY:
        count, seconds = 24, _HOUR
    elif range_ == WEEK:
        count, seconds = 7, _DAY
    elif range_ == FOUR_WEEKS:
        count, seconds = 28, _DAY
    elif range_ == TWELVE_MONTHS:
        count, seconds = 12, 30 * _DAY
    else:  # ALLTIME
        count, seconds = 5, 365 * _DAY  # floored at the same 5yr lookback as R9.

    span = timedelta(seconds=seconds * count)
    first_start = now - span
    bucket_starts = [first_start + timedelta(seconds=seconds * i) for i in range(count)]
    return bucket_starts, seconds, first_start.date(), now.date()


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
    bucket_starts, bucket_seconds, window_start, window_end = bucket_plan(range_, now)

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
    s3 = [a - d for a, d in zip(s1, s2)]
    s4 = s4_series(conn, org_id, bucket_starts, bucket_seconds)

    github_audit.record_productivity_request(
        duration_ms=(time.perf_counter() - started_at) * 1000,
        points_spent=int(activity.get("points_spent") or 0),
        cache_hit=False,
        truncated=bool(activity.get("truncated")),
    )

    return {
        "range": range_,
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
    """
    now = now or datetime.now(timezone.utc)
    cached = productivity_cache.get(org_id, user_key, range_, now=now.timestamp())
    if cached is not None:
        github_audit.record_productivity_request(
            duration_ms=0.0,
            points_spent=0,
            cache_hit=True,
            truncated=bool(cached.get("truncated")),
        )
        return cached

    result = build_series(conn, org_id, range_, now=now)
    result["computed_at"] = now.isoformat()
    productivity_cache.put(org_id, user_key, range_, result, now=now.timestamp())
    return result
