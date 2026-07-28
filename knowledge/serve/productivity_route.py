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

import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from knowledge.serve import github_audit, github_commits, github_token, productivity_cache
from knowledge.serve.auth import Principal
from knowledge.serve.productivity_attribution import bucketed_owner_totals, net_lines
from knowledge.serve.orgs_store import OrgsStore
from knowledge.serve.productivity_series import (
    s4_instrumentation_date,
    s4_series,
    s4_series_by_org,
)
from knowledge.serve.spaces_store import SpacesStore

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
_MONTH = 30 * _DAY

# The bucket widths a caller may explicitly request via ``bucket_unit`` (R-bin-by): "hour" is
# deliberately excluded -- it stays an implicit-only default for the ``day`` range, never an
# explicit override for any range.
ALLOWED_BUCKET_UNITS = {"day", "week", "month"}

_BUCKET_SECONDS_BY_UNIT = {
    "hour": _HOUR,
    "day": _DAY,
    "week": _WEEK,
    "month": _MONTH,
}

# The timezone every bucket boundary is aligned to (D-bucket-tz). Buckets start at LOCAL
# midnight (local top-of-hour for hourly buckets) in this zone, never at the wall-clock
# instant the request happened to arrive.
#
# Before this existed, `bucket_plan` anchored buckets at `now - span` and stepped forward,
# so a request at 10:23 PM produced "daily" buckets running 10:23 PM -> 10:23 PM. The bucket
# LABELLED "Jul 26" actually covered Jul 26 22:23 -> Jul 27 22:23, so a full day of work done
# on Jul 27 was rendered under Jul 26's bar and Jul 27 read as empty -- which is exactly how
# this was reported (2026-07-28). The static caveat shown under the chart has always CLAIMED
# "bucket boundaries are fixed to America/Denver and never vary by viewer"; that claim only
# became true with this change.
DEFAULT_BUCKET_TIMEZONE = "America/Denver"

DEFAULT_OWNER_LOGIN = "MatthewDaw"
DEFAULT_OWNER_EMAIL = "mattdaw7@gmail.com"

# A second real identity for the same person: several local git installs (the sotos
# checkout, this laptop's global fallback) never had `user.email` configured, so git
# fell back to its own `user@hostname` default instead of the verified GitHub email.
# GitHub's GraphQL commit author has no `user` (the email isn't linked to any GitHub
# account) for these, so without this they were silently `unattributed` -- confirmed
# via the live GraphQL API: 124 real commits to MatthewDaw/praxis in a 20-day window
# all carried this exact author email and vanished from S1 entirely (2026-07-28).
DEFAULT_OWNER_EMAILS_EXTRA = ["matthewdaw@Matthews-MacBook-Air.local"]

# The repos S1-S3's GitHub commit-activity fetch queries, as an explicit, statically configured
# list rather than something discovered per request (GitHub's GraphQL ``contributionsCollection``
# discovery silently omits private repos owned by an org the account is merely a member of -- see
# ``github_commits`` module docstring -- so discovery was replaced with this config).
DEFAULT_TRACKED_REPOS = [
    "MatthewDaw/praxis",
    "MatthewDaw/agent_factory",
    "MatthewDaw/appeal_engine",
    "Daw-Code-Farms-Inc/sotos",
    "Bestie-Labs-Inc/bestie",
]

# The three GitHub-token key statuses the panel must distinguish (R21): no token
# configured at all, a token GitHub outright rejects (401 -- invalid/revoked/expired),
# and a token GitHub recognizes but that lacks the permission the query needs (403,
# e.g. missing Contents: Read). Each is reported as ``{"key_status": ...}`` instead of
# raising -- the route must never surface a raw 401/403 to the caller.
KEY_STATUS_MISSING = "missing"
KEY_STATUS_EXPIRED = "expired"
KEY_STATUS_INSUFFICIENT_SCOPE = "insufficient_scope"


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
    env_extra = [
        e.strip()
        for e in os.environ.get("PRODUCTIVITY_OWNER_EMAILS", "").split(",")
        if e.strip()
    ]
    extra = env_extra or DEFAULT_OWNER_EMAILS_EXTRA
    return [primary, *extra]


def tracked_repos() -> list[str]:
    """The explicit ``"owner/name"`` repo list S1-S3's GitHub commit-activity fetch queries.

    ``PRODUCTIVITY_TRACKED_REPOS`` (comma-separated ``"owner/name"`` pairs) overrides
    :data:`DEFAULT_TRACKED_REPOS`, mirroring the env-var-with-default idiom
    :func:`owner_login`/:func:`owner_emails` already use for this feature's other
    account-wide GitHub config. Replaces GraphQL-based repo discovery (see
    ``github_commits`` module docstring for why discovery was unreliable).
    """
    raw = os.environ.get("PRODUCTIVITY_TRACKED_REPOS", "").strip()
    if not raw:
        return list(DEFAULT_TRACKED_REPOS)
    return [r.strip() for r in raw.split(",") if r.strip()]


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


def bucket_timezone() -> ZoneInfo:
    """The zone every bucket boundary aligns to (see :data:`DEFAULT_BUCKET_TIMEZONE`).

    ``PRODUCTIVITY_BUCKET_TIMEZONE`` overrides it, mirroring the env-var-with-default idiom
    this module's other config uses. It is deliberately a SERVER-side setting: no
    client-supplied zone is ever accepted (see this module's docstring and the
    ``no-client-supplied-timezone`` build check), so boundaries can never vary per caller.
    An unknown zone name falls back to the default rather than 500-ing every request.
    """
    name = os.environ.get("PRODUCTIVITY_BUCKET_TIMEZONE", "").strip() or DEFAULT_BUCKET_TIMEZONE
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_BUCKET_TIMEZONE)


def _floor_to_local_boundary(local_dt: datetime, unit: str) -> datetime:
    """Truncate ``local_dt`` (already in the bucket zone) down to the start of its ``unit``.

    Hourly buckets floor to the top of the hour; every other width floors to local midnight.
    ``week``/``month`` deliberately floor to midnight rather than to Monday/the 1st: buckets
    are fixed-width (``bucket_seconds`` is a single scalar every downstream counter relies on
    -- see ``productivity_series.bucket_counts``), so they are rolling 7-day/30-day windows
    ending today, not calendar weeks or calendar months, and pretending otherwise by snapping
    to a Monday would misdate every bucket by up to six days.
    """
    if unit == "hour":
        return local_dt.replace(minute=0, second=0, microsecond=0)
    return local_dt.replace(hour=0, minute=0, second=0, microsecond=0)


def bucket_plan(
    range_: str, now: datetime, bucket_unit: str = "",
) -> tuple[list[datetime], float, date, date, str]:
    """Return ``(bucket_starts, bucket_seconds, window_start, window_end, bucket_unit)`` for
    ``range_`` (R8), optionally overriding the bucket width via ``bucket_unit`` (R-bin-by).

    Each range has a fixed WINDOW SPAN (24h/7d/28d/~364d/~5yr) that never changes. By default
    (``bucket_unit`` omitted/empty) each range also selects its traditional bucket width so
    every selectable range yields enough points to draw a line: hourly for ``day``, daily for
    ``week``/``4weeks``, weekly for ``12months``, monthly for ``alltime`` -- this is UNCHANGED
    from before ``bucket_unit`` existed. When ``bucket_unit`` is explicitly one of
    ``ALLOWED_BUCKET_UNITS`` (``"day"``/``"week"``/``"month"`` -- never ``"hour"``, which stays
    an implicit-only default for the ``day`` range), it overrides the default width and
    ``count`` is recomputed as ``ceil(span_seconds / bucket_seconds_for(unit))`` so the same
    window span is covered by buckets of the requested width.

    The returned ``bucket_unit`` names the width actually used (``"hour"``/``"day"``/``"week"``/
    ``"month"``) so the response can disclose it and the client can label the axis in matching
    units. Buckets are contiguous, equal-width, half-open windows; each bucket SUMS the values
    of the periods it contains (never averages) — a property of how callers accumulate into
    these windows, not of the plan itself. ``window_start``/``window_end`` are the calendar-date
    bounds the GitHub commit-activity fetch spans, ``window_end`` being the last bucket's END
    (local midnight tonight) so today's own commits fall inside the queried window.

    Boundaries are aligned to LOCAL time in :func:`bucket_timezone` — the most recent bucket
    starts at local midnight today (local top-of-hour when hourly) and holds everything since,
    rather than starting at the wall-clock instant the request arrived. See
    :data:`DEFAULT_BUCKET_TIMEZONE` for the bug that alignment fixes.

    Known edge: buckets stay fixed-width, so on the two DST-transition days a "daily" bucket
    spans 23 or 25 local hours and subsequent boundaries sit an hour off local midnight until
    the next request re-anchors them. Making those calendar-exact would require variable-width
    buckets, which ``bucket_seconds`` (a single scalar every downstream counter consumes) does
    not model; the ≤1h drift on 2 days a year is the deliberate trade.
    """
    if range_ not in ALLOWED_RANGES:
        raise ValueError(f"unknown range {range_!r}")
    if bucket_unit and bucket_unit not in ALLOWED_BUCKET_UNITS:
        raise ValueError(f"unknown bucket_unit {bucket_unit!r}")

    if range_ == DAY:
        count, seconds, unit = 24, _HOUR, "hour"
    elif range_ == WEEK:
        count, seconds, unit = 7, _DAY, "day"
    elif range_ == FOUR_WEEKS:
        count, seconds, unit = 28, _DAY, "day"
    elif range_ == TWELVE_MONTHS:
        count, seconds, unit = 52, _WEEK, "week"
    else:  # ALLTIME
        count, seconds, unit = 60, _MONTH, "month"  # 5yr lookback (same floor as R9).

    span_seconds = seconds * count
    if bucket_unit and bucket_unit != unit:
        unit = bucket_unit
        seconds = _BUCKET_SECONDS_BY_UNIT[unit]
        count = math.ceil(span_seconds / seconds)

    # Anchor on the LOCAL boundary of the bucket `now` falls in, then walk backwards, so
    # every bucket starts at local midnight (local top-of-hour when hourly) instead of at
    # whatever wall-clock instant the request arrived. See DEFAULT_BUCKET_TIMEZONE.
    local_now = now.astimezone(bucket_timezone())
    last_start = _floor_to_local_boundary(local_now, unit)
    bucket_starts = [
        last_start - timedelta(seconds=seconds * (count - 1 - i)) for i in range(count)
    ]
    first_start = bucket_starts[0]
    # `window_end` is the LAST bucket's end, not `now`: the current bucket runs to local
    # midnight tonight, and the GitHub fetch must span it or today's own commits fall
    # outside the queried window and vanish from the bucket that is meant to hold them.
    window_end = last_start + timedelta(seconds=seconds)
    return bucket_starts, seconds, first_start.date(), window_end.date(), unit


def _series_points(bucket_starts: list[datetime], values: list[int]) -> list[dict[str, Any]]:
    return [
        {"bucket_start": start.isoformat(), "value": value}
        for start, value in zip(bucket_starts, values)
    ]


def s4_orgs(conn: Any, org_id: str, user_id: str) -> list[dict[str, str]]:
    """Every org S4 aggregates over: all orgs ``user_id`` belongs to, plus ``org_id`` itself.

    S4 counts the REQUESTING PERSON's completed tickets, so it must not be scoped to the
    one org ``X-Praxis-Org`` selected — a user who belongs to several orgs finishes tickets
    in whichever one the work lives in, and scoping to the active org reported those real
    completions as a flat zero (the reason this function exists).

    The active ``org_id`` is always included even if membership lookup can't see it (an
    empty/unknown ``user_id``, or a principal reaching an org through some path other than
    ``org_members``), so this can only ever WIDEN the old single-org behavior, never narrow
    it. Returned as ``{"org_id", "name"}`` dicts, ordered with the active org first.
    """
    memberships = OrgsStore(conn).list_orgs(user_id) if user_id else []
    by_id = {m["org_id"]: (m.get("name") or m["org_id"]) for m in memberships}
    by_id.setdefault(org_id, org_id)
    ordered = [org_id, *(oid for oid in by_id if oid != org_id)]
    return [{"org_id": oid, "name": by_id[oid]} for oid in ordered]


def build_series(
    conn: Any, org_id: str, range_: str, *, user_id: str = "", now: datetime | None = None,
    bucket_unit: str = "",
) -> dict[str, Any]:
    """Assemble the four named series for ``range_``, scoped to ``org_id`` (S1-S3, spaces_count)
    and to every org ``user_id`` belongs to (S4).

    Fetches the owner's GitHub commit activity for the resolved window (S1 additions, S2
    deletions, S3 their difference) and the finished-ticket counts (S4, R7),
    bucketed identically so every series lines up on the same ``bucket_start`` axis.

    S4 IS NOT SCOPED TO ``org_id``. ``series.s4_tickets_completed`` is the SUM across every
    org ``user_id`` belongs to (see :func:`s4_orgs`), and ``s4_instrumentation_date`` is the
    earliest finish across that same set. Scoping S4 to the active org made tickets the user
    genuinely completed in their OTHER orgs render as "no tickets completed" — the bug this
    behavior fixes. ``org_id`` still scopes ``spaces_count`` and remains part of the cache key.

    ``series_by_org`` breaks S4 down per org, mirroring how ``series_by_repo`` breaks S1-S3
    down per repo::

        "series_by_org": {
            "<org_id>": {
                "name": "<org display name, falling back to the org id>",
                "s4_tickets_completed": [{"bucket_start": "<iso8601>", "value": <int>}, ...]
            },
            ...
        }

    Every org the user belongs to gets an entry, INCLUDING orgs whose counts are all zero
    (a confirmed zero is real information; hiding it is the client's decision, not this
    layer's). The per-bucket values sum position-wise to the aggregate
    ``series.s4_tickets_completed``. When the S4 computation fails, ``series_by_org`` is an
    empty dict and the failure is reported under ``errors.s4_tickets_completed`` — never an
    all-zero breakdown that would be indistinguishable from genuine inactivity.

    When the backend's GitHub token is missing, expired/revoked, or lacks the required
    scope (R21), no series are computed and no raw 401/403 ever escapes: the result is
    instead ``{"key_status": KEY_STATUS_MISSING | KEY_STATUS_EXPIRED |
    KEY_STATUS_INSUFFICIENT_SCOPE}``, naming exactly which of the three applies. An
    expired token also invalidates the in-process token cache so the very next call
    re-fetches (picking up a rotated secret with no redeploy, per ``github_token``).

    Also reports ``repos_discovered``/``spaces_count`` (R20): the count of GitHub
    repositories the commit-activity discovery found and the count of Praxis spaces
    in ``org_id``. Zero/zero is the first-run signal the client uses to show a
    dedicated "nothing connected yet" state instead of a flat zero-valued chart that
    would otherwise look indistinguishable from "connected but did no work".

    ``series_by_repo`` breaks S1-S3 down per tracked repo (keyed by ``"owner/name"``,
    same shape as ``series`` minus S4 which has no per-repo meaning) for every repo that
    actually returned commit data this call -- a repo omitted from ``activity["repositories"]``
    (e.g. its history fetch failed) is simply absent here too, never reported as a
    confirmed-zero repo. The aggregate ``series`` above always stays the sum across every
    repo, so a caller keeps a single "total" chart plus one small chart per repo.

    The two data sources fail independently: if the ticket series (S4, Praxis-derived)
    raises, S1-S3 (the git series, already fetched above) still render normally and the
    response instead carries ``errors.s4_tickets_completed.reason`` naming the failure -- a
    failed series must never be silently reported as a confirmed flat zero (indistinguishable
    from genuine zero activity).
    """
    started_at = time.perf_counter()
    now = now or datetime.now(timezone.utc)
    bucket_starts, bucket_seconds, window_start, window_end, bucket_unit = bucket_plan(
        range_, now, bucket_unit
    )

    token = github_token.resolve_github_token()
    if token is None:
        return {"key_status": KEY_STATUS_MISSING}

    try:
        activity = github_commits.fetch_commit_activity(
            tracked_repos(), window_start, window_end, token=token
        )
    except github_commits.GitHubAuthExpired:
        github_token.invalidate_github_token()
        return {"key_status": KEY_STATUS_EXPIRED}
    except github_commits.GitHubInsufficientScope:
        return {"key_status": KEY_STATUS_INSUFFICIENT_SCOPE}

    github_audit.record_github_use("/productivity", len(activity.get("repositories") or {}))

    repositories = activity.get("repositories") or {}
    totals = bucketed_owner_totals(
        repositories,
        owner_login(),
        bucket_starts,
        bucket_seconds,
        owner_emails=owner_emails(),
    )
    s1, s2 = totals["s1"], totals["s2"]
    s3 = net_lines(s1, s2)

    # Per-repo breakdown (S1-S3 only, alongside the aggregate above): one repo -> commits
    # dict at a time through the SAME attribution/bucketing logic, so a caller can chart
    # each tracked repo that actually has data individually, not just the summed total.
    series_by_repo: dict[str, dict[str, Any]] = {}
    for repo, commits in repositories.items():
        repo_totals = bucketed_owner_totals(
            {repo: commits}, owner_login(), bucket_starts, bucket_seconds, owner_emails=owner_emails()
        )
        repo_s1, repo_s2 = repo_totals["s1"], repo_totals["s2"]
        series_by_repo[repo] = {
            "s1_lines_added": _series_points(bucket_starts, repo_s1),
            "s2_lines_deleted": _series_points(bucket_starts, repo_s2),
            "s3_net_lines": _series_points(bucket_starts, net_lines(repo_s1, repo_s2)),
        }

    # S4, across EVERY org the requesting user belongs to (not just the active one) --
    # aggregate, per-org breakdown and instrumentation date all read the same org set, so
    # a failure in any of them isolates the whole of S4 rather than half-reporting it.
    errors: dict[str, dict[str, str]] = {}
    series_by_org: dict[str, dict[str, Any]] = {}
    instrumentation_date = None
    try:
        orgs = s4_orgs(conn, org_id, user_id)
        org_ids = [o["org_id"] for o in orgs]
        s4 = s4_series(conn, org_ids, bucket_starts, bucket_seconds)
        by_org = s4_series_by_org(conn, org_ids, bucket_starts, bucket_seconds)
        series_by_org = {
            o["org_id"]: {
                "name": o["name"],
                "s4_tickets_completed": _series_points(
                    bucket_starts, by_org.get(o["org_id"], [0] * len(bucket_starts))
                ),
            }
            for o in orgs
        }
        instrumentation_date = s4_instrumentation_date(conn, org_ids)
    except Exception as exc:  # noqa: BLE001 - the ticket series must never take down S1-S3
        s4 = []
        series_by_org = {}
        instrumentation_date = None
        errors["s4_tickets_completed"] = {"reason": str(exc)}

    github_audit.record_productivity_request(
        duration_ms=(time.perf_counter() - started_at) * 1000,
        points_spent=int(activity.get("points_spent") or 0),
        cache_hit=False,
        truncated=bool(activity.get("truncated")),
    )

    result: dict[str, Any] = {
        "range": range_,
        "bucket_unit": bucket_unit,
        "truncated": bool(activity.get("truncated")),
        "s4_instrumentation_date": instrumentation_date,
        "repos_discovered": len(activity.get("repositories") or {}),
        "spaces_count": len(SpacesStore(conn).list_spaces(org_id)),
        "series": {
            "s1_lines_added": _series_points(bucket_starts, s1),
            "s2_lines_deleted": _series_points(bucket_starts, s2),
            "s3_net_lines": _series_points(bucket_starts, s3),
            "s4_tickets_completed": _series_points(bucket_starts, s4),
        },
        "series_by_repo": series_by_repo,
        "series_by_org": series_by_org,
    }
    if errors:
        result["errors"] = errors
    return result


def get_series_cached(
    conn: Any, org_id: str, user_key: str, range_: str, *, now: datetime | None = None,
    force: bool = False, bucket_unit: str = "",
) -> dict[str, Any]:
    """Serve ``/productivity`` from the short/long-TTL cache when possible (R4).

    ``user_key`` is the requesting principal's user id (``Principal.sub``, threaded in from
    the route's ``active_user_id`` dependency). It is both a cache-key dimension AND the
    identity :func:`build_series` enumerates orgs for, so S4 spans every org the caller
    belongs to rather than only the active one.

    Cached on ``(org_id, user_key, range_, bucket_unit)`` — the caller's org/identity, the
    requested window, and the requested bucket width (empty string for "use the range's
    default", so today's behavior and cache entries are unchanged when the caller never
    passes ``bucket_unit``) — never a client-supplied timezone (see module docstring). The
    key still discriminates correctly now that S4 depends on the caller's FULL org set:
    that set is a function of ``user_key`` alone, which is already in the key, so two
    callers with different org sets are necessarily different users and necessarily
    different keys. (A membership change for the SAME user is not reflected until the entry
    ages out of its TTL band — the same eventual-consistency window the cache already
    accepts for every other input, and bounded at 90s/20min.) A
    hit returns the EXACT prior payload — same ``computed_at`` — and never calls
    GitHub or Praxis again; a miss calls :func:`build_series`, stamps
    ``computed_at`` onto the result, and caches it for that range's TTL band (D7).
    Either path logs one observability record (R40): the miss path logs from
    inside :func:`build_series`, the hit path logs here (zero GitHub points spent).

    ``force=True`` (the Refresh control's explicit-force affordance, R33) skips
    the cache READ entirely and always recomputes -- but still WRITES the fresh
    result back into the cache for that range's TTL band, so the next
    non-forced request still benefits from it.

    A ``key_status`` result (R21 -- the token is missing/expired/insufficient-scope)
    is NEVER cached: an operator fixing the token must see it take effect on the very
    next request, not wait out a TTL band that was sized for real series data.

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
    cached = (
        None if force else productivity_cache.get(org_id, user_key, range_, bucket_unit, now=ts)
    )
    if cached is not None:
        github_audit.record_productivity_request(
            duration_ms=0.0,
            points_spent=0,
            cache_hit=True,
            truncated=bool(cached.get("truncated")),
        )
        return cached

    lock = productivity_cache.lock_for(org_id, user_key, range_, bucket_unit)
    with lock:
        # Re-check: another thread may have populated the cache while we were
        # waiting for the lock -- that thread's payload wins, we never recompute
        # (unless the caller explicitly forced a fresh fetch).
        cached = (
            None if force
            else productivity_cache.get(org_id, user_key, range_, bucket_unit, now=ts)
        )
        if cached is not None:
            github_audit.record_productivity_request(
                duration_ms=0.0,
                points_spent=0,
                cache_hit=True,
                truncated=bool(cached.get("truncated")),
            )
            return cached

        result = build_series(
            conn, org_id, range_, user_id=user_key, now=now, bucket_unit=bucket_unit
        )
        if result.get("key_status"):
            return result
        if result.get("reason") is not None:
            stale = productivity_cache.get_stale(org_id, user_key, range_, bucket_unit)
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
        productivity_cache.put(org_id, user_key, range_, bucket_unit, result, now=ts)
        return result
