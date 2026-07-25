"""GitHub GraphQL client for the productivity feature's commit-activity fetch (R2/R3/R37).

Given a date window, returns per-repository commit activity — each commit node carrying
``additions``, ``deletions``, ``committedDate`` and the author's ``login`` — by issuing exactly
one *discovery* GraphQL query per calendar-year chunk of the window (GitHub's
``contributionsCollection`` rejects a ``from``/``to`` span over one year, so discovery must be
chunked) to find which repositories the account was active in, then exactly one *history* GraphQL
query per repository discovered active in ANY chunk to pull its commits for the whole window.

The caller supplies the GitHub token (this module never resolves or caches one itself, and never
logs it, writes it to the graph, or puts it in a return value) and the date window as plain
calendar dates — this module never reads a client-supplied timezone/offset (bucket boundaries
belong to the caller, not this client).

RELIABILITY (R37): every call is bounded by a timeout and retried with exponential backoff. A
GitHub timeout, upstream 5xx, secondary rate limit (honoring ``Retry-After``), or a partial
GraphQL ``errors`` payload never gets silently reported as zero activity for the affected window —
:func:`fetch_commit_activity` instead returns ``truncated=True`` with a :class:`TruncationReason`
constant naming the cause, keeping whatever partial data was already fetched.
"""

from __future__ import annotations

import calendar
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"

# range=alltime never scans further back than this many years, even for an account created earlier.
ALLTIME_MAX_LOOKBACK_YEARS = 5

# (query, variables, token) -> parsed JSON response body (``{"data": {...}}``).
Transport = Callable[[str, dict[str, Any], Optional[str]], dict[str, Any]]

DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY_S = 1.0
DEFAULT_MAX_DELAY_S = 30.0
DEFAULT_TIMEOUT_S = 30.0


class TruncationReason:
    """Enumerated reasons :func:`fetch_commit_activity` can report ``truncated=True`` for.

    Each is a stable string constant (never a raw exception message) so a caller — including a
    future HTTP route — can switch on it directly.
    """

    TIMEOUT = "timeout"
    UPSTREAM_ERROR = "upstream_error"
    RATE_LIMITED = "rate_limited"
    PARTIAL_ERRORS = "partial_errors"


class GitHubTransportError(Exception):
    """Base for the transport-classified failures the retry loop understands."""


class GitHubTimeout(GitHubTransportError):
    """The outbound call exceeded its bounded timeout."""


class GitHubUpstreamError(GitHubTransportError):
    """GitHub returned a 5xx (e.g. a 502) — an upstream, not a client, error."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"GitHub upstream error: {status_code}")
        self.status_code = status_code


class GitHubRateLimited(GitHubTransportError):
    """A secondary rate limit response, carrying the ``Retry-After`` seconds to honor."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"GitHub secondary rate limit, retry after {retry_after}s")
        self.retry_after = retry_after


_REASON_FOR_ERROR: dict[type[GitHubTransportError], str] = {
    GitHubTimeout: TruncationReason.TIMEOUT,
    GitHubUpstreamError: TruncationReason.UPSTREAM_ERROR,
    GitHubRateLimited: TruncationReason.RATE_LIMITED,
}

DISCOVERY_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      commitContributionsByRepository(maxRepositories: 100) {
        repository { nameWithOwner }
      }
    }
  }
  rateLimit { cost }
}
"""

HISTORY_QUERY = """
query($owner: String!, $name: String!, $since: GitTimestamp!, $until: GitTimestamp!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(since: $since, until: $until, first: 100) {
            nodes {
              additions
              deletions
              committedDate
              author { user { login } }
            }
          }
        }
      }
    }
  }
  rateLimit { cost }
}
"""


def _points_cost(data: Optional[dict[str, Any]]) -> int:
    """The GraphQL ``rateLimit.cost`` (points spent) a response reports, or 0.

    Every discovery/history query above requests ``rateLimit { cost }`` alongside its real
    payload; a response predating that field (or a test fixture that omits it) simply spends 0.
    """
    return int(((data or {}).get("data") or {}).get("rateLimit", {}).get("cost") or 0)


def year_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split ``[start, end]`` into contiguous per-calendar-year ``(start, end)`` pairs.

    GitHub's ``contributionsCollection`` rejects a ``from``/``to`` span over one year, so the
    discovery query must be issued once per calendar year the window touches.
    """
    if start > end:
        raise ValueError("start must not be after end")
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        chunk_end = min(date(cur.year, 12, 31), end)
        chunks.append((cur, chunk_end))
        cur = date(cur.year + 1, 1, 1)
    return chunks


def _add_months(d: date, months: int) -> date:
    """Return ``d`` shifted forward by ``months`` calendar months, clamping the day-of-month."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def resolve_alltime_start(account_created_at: date, now: date, max_lookback_years: int = ALLTIME_MAX_LOOKBACK_YEARS) -> date:
    """Floor ``range=alltime`` at the later of the account's creation date or ``max_lookback_years`` back.

    An unbounded historical scan is never issued: the effective start is never earlier than
    ``now`` minus ``max_lookback_years``, even for an account created before that date.
    """
    floor = _add_months(now, -12 * max_lookback_years)
    return max(account_created_at, floor)


def months_span(start: date, end: date) -> int:
    """Whole calendar months elapsed from ``start`` to ``end`` (inclusive of a same-day partial month)."""
    if start > end:
        raise ValueError("start must not be after end")
    span = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day >= start.day:
        span += 1
    return span


def alltime_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split ``[start, end]`` into rolling 12-month chunks starting at ``start``.

    Unlike ``year_chunks`` (calendar-year aligned), these chunks start at ``start`` itself so the
    chunk count is always exactly ``ceil(months_span(start, end) / 12)`` regardless of where in the
    year the all-time window begins.
    """
    if start > end:
        raise ValueError("start must not be after end")
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        nxt = _add_months(cur, 12)
        chunk_end = min(nxt - timedelta(days=1), end)
        chunks.append((cur, chunk_end))
        cur = nxt
    return chunks


def _iso(d: date) -> str:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _default_transport(
    query: str, variables: dict[str, Any], token: Optional[str], *, timeout: float = DEFAULT_TIMEOUT_S
) -> dict[str, Any]:
    """The real ``httpx`` POST, bounded by ``timeout`` and translating known failure shapes into
    the classified :class:`GitHubTransportError` subclasses the retry loop understands."""
    import httpx

    headers = {"Authorization": f"bearer {token}"} if token else {}
    try:
        resp = httpx.post(
            GITHUB_GRAPHQL_URL,
            json={"query": query, "variables": variables},
            headers=headers,
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        raise GitHubTimeout(str(exc)) from exc

    if resp.status_code in (403, 429) and "retry-after" in resp.headers:
        raise GitHubRateLimited(retry_after=float(resp.headers["retry-after"]))
    if 500 <= resp.status_code < 600:
        raise GitHubUpstreamError(resp.status_code)
    resp.raise_for_status()
    return resp.json()


Sleep = Callable[[float], None]


def _call_with_retry(
    transport: Transport,
    query: str,
    variables: dict[str, Any],
    token: Optional[str],
    *,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    sleep: Sleep,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Call ``transport`` with bounded retry + exponential backoff.

    Returns ``(response, None)`` on success, or ``(None, reason)`` — a :class:`TruncationReason`
    constant — once ``max_retries`` retries are exhausted. A ``GitHubRateLimited`` failure backs
    off for AT LEAST the ``Retry-After`` seconds it carries (never less); other failures back off
    with a plain exponential schedule (``base_delay * 2**attempt``, capped at ``max_delay``).
    """
    attempt = 0
    while True:
        try:
            return transport(query, variables, token), None
        except GitHubTransportError as exc:
            reason = _REASON_FOR_ERROR[type(exc)]
            if attempt >= max_retries:
                return None, reason
            backoff = min(base_delay * (2**attempt), max_delay)
            if isinstance(exc, GitHubRateLimited):
                backoff = max(backoff, exc.retry_after)
            sleep(backoff)
            attempt += 1


def _discover_active_repos(
    login: str,
    chunks: list[tuple[date, date]],
    transport: Transport,
    token: Optional[str],
    *,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    sleep: Sleep,
) -> tuple[list[str], Optional[str], int]:
    """Issue one discovery query per year-chunk; return (active repos, truncation reason, points spent).

    Active repos are deduped in first-seen order. A chunk whose call ultimately fails (retries
    exhausted) contributes no repos and sets the returned reason — that chunk's activity is
    UNKNOWN, never folded in as a confirmed zero. A response carrying both ``data`` and ``errors``
    (partial GraphQL failure) still contributes whatever repos it found, and also sets the reason.
    """
    seen: dict[str, None] = {}
    reason: Optional[str] = None
    points = 0
    for since, until in chunks:
        variables = {"login": login, "from": _iso(since), "to": _iso(until)}
        data, call_reason = _call_with_retry(
            transport, DISCOVERY_QUERY, variables, token,
            max_retries=max_retries, base_delay=base_delay, max_delay=max_delay, sleep=sleep,
        )
        if call_reason is not None:
            reason = reason or call_reason
            continue
        assert data is not None
        points += _points_cost(data)
        if data.get("errors") and data.get("data") is not None:
            reason = reason or TruncationReason.PARTIAL_ERRORS
        contributions = (
            (data.get("data") or {}).get("user") or {}
        ).get("contributionsCollection") or {}
        entries = contributions.get("commitContributionsByRepository") or []
        for entry in entries:
            name = (entry.get("repository") or {}).get("nameWithOwner")
            if name and name not in seen:
                seen[name] = None
    return list(seen.keys()), reason, points


def _fetch_repo_commits(
    repo: str,
    start: date,
    end: date,
    transport: Transport,
    token: Optional[str],
    *,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    sleep: Sleep,
) -> tuple[Optional[list[dict[str, Any]]], Optional[str], int]:
    """Issue one history query for ``repo``; return (commit nodes or None, truncation reason, points spent).

    ``None`` (not ``[]``) signals the repo's activity is UNKNOWN because the call ultimately
    failed — the caller must omit the repo rather than report it as a confirmed zero.
    """
    owner, _, name = repo.partition("/")
    variables = {"owner": owner, "name": name, "since": _iso(start), "until": _iso(end)}
    data, reason = _call_with_retry(
        transport, HISTORY_QUERY, variables, token,
        max_retries=max_retries, base_delay=base_delay, max_delay=max_delay, sleep=sleep,
    )
    if reason is not None:
        return None, reason, 0
    assert data is not None
    points = _points_cost(data)
    if data.get("errors") and data.get("data") is not None:
        reason = TruncationReason.PARTIAL_ERRORS
    repo_data = (data.get("data") or {}).get("repository") or {}
    target = (repo_data.get("defaultBranchRef") or {}).get("target") or {}
    nodes = (target.get("history") or {}).get("nodes") or []
    commits = [
        {
            "additions": node.get("additions"),
            "deletions": node.get("deletions"),
            "committedDate": node.get("committedDate"),
            "author_login": ((node.get("author") or {}).get("user") or {}).get("login"),
        }
        for node in nodes
    ]
    return commits, reason, points


def fetch_commit_activity(
    login: str,
    start: date,
    end: date,
    *,
    token: Optional[str] = None,
    transport: Optional[Transport] = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY_S,
    max_delay: float = DEFAULT_MAX_DELAY_S,
    sleep: Sleep = time.sleep,
) -> dict[str, Any]:
    """Fetch per-repository commit activity for ``login`` over the calendar-date window ``[start, end]``.

    Issues exactly one discovery query per calendar-year chunk of the window (to find which
    repositories ``login`` committed to), then exactly one history query per repository found
    active in any chunk (to pull its commits for the full window).

    Returns ``{"repositories": {...}, "truncated": bool, "reason": str|None, "points_spent": int}``:
    ``repositories`` is keyed by ``"owner/name"`` -> a list of commit dicts (``additions``,
    ``deletions``, ``committedDate``, ``author_login``). ``truncated``/``reason`` surface a
    GitHub timeout, upstream 5xx, secondary rate limit, or partial GraphQL ``errors`` payload
    (see :class:`TruncationReason`) that survived ``max_retries`` retries with exponential
    backoff — the affected repo/window is OMITTED from ``repositories`` rather than reported as a
    confirmed zero, and any repo/window that succeeded is still included with real data.
    ``points_spent`` is the summed GraphQL ``rateLimit.cost`` every issued query reported (0 for a
    response that predates that field), for the productivity route's observability log (R40).

    ``token`` authenticates the outbound GraphQL request (the caller resolves/rotates it; this
    module never caches or logs it). ``transport`` is injectable (query, variables, token) ->
    parsed JSON body, defaulting to a real ``httpx`` POST against the GitHub GraphQL API bounded
    by a timeout. ``sleep`` is injectable so tests never actually wait out a backoff.
    """
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")
    xport: Transport = transport or _default_transport
    retry_kwargs = dict(max_retries=max_retries, base_delay=base_delay, max_delay=max_delay, sleep=sleep)

    chunks = year_chunks(start, end)
    active_repos, reason, points = _discover_active_repos(login, chunks, xport, token, **retry_kwargs)

    repositories: dict[str, list[dict[str, Any]]] = {}
    for repo in active_repos:
        commits, repo_reason, repo_points = _fetch_repo_commits(
            repo, start, end, xport, token, **retry_kwargs
        )
        points += repo_points
        if repo_reason is not None:
            reason = reason or repo_reason
        if commits is not None:
            repositories[repo] = commits

    return {
        "repositories": repositories,
        "truncated": reason is not None,
        "reason": reason,
        "points_spent": points,
    }


def fetch_commit_activity_alltime(
    login: str,
    account_created_at: date,
    now: date,
    *,
    token: Optional[str] = None,
    transport: Optional[Transport] = None,
    max_lookback_years: int = ALLTIME_MAX_LOOKBACK_YEARS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY_S,
    max_delay: float = DEFAULT_MAX_DELAY_S,
    sleep: Sleep = time.sleep,
) -> tuple[date, dict[str, list[dict[str, Any]]]]:
    """Fetch per-repository commit activity for ``range=alltime``, floored at a bounded lookback.

    The scan never goes back further than ``max_lookback_years`` before ``now`` even when the
    account is older than that: ``effective_start`` is the later of ``account_created_at`` and
    ``now`` minus ``max_lookback_years``. Discovery is chunked in rolling 12-month windows from
    ``effective_start`` (not calendar-year aligned), so exactly ``ceil(months_span(effective_start,
    now) / 12)`` discovery queries are issued, plus at most one history query per repository
    discovered active in any chunk.

    Like :func:`fetch_commit_activity`, every call is bounded/retried (R37) and a repo/chunk whose
    call ultimately fails is simply omitted rather than reported as a confirmed zero.

    Returns ``(effective_start, commit_activity)`` so the caller/UI can label the floored window.
    """
    xport: Transport = transport or _default_transport
    retry_kwargs = dict(max_retries=max_retries, base_delay=base_delay, max_delay=max_delay, sleep=sleep)
    effective_start = resolve_alltime_start(account_created_at, now, max_lookback_years)
    chunks = alltime_chunks(effective_start, now)
    active_repos, _reason, _points = _discover_active_repos(login, chunks, xport, token, **retry_kwargs)

    commits: dict[str, list[dict[str, Any]]] = {}
    for repo in active_repos:
        repo_commits, _repo_reason, _repo_points = _fetch_repo_commits(
            repo, effective_start, now, xport, token, **retry_kwargs
        )
        if repo_commits is not None:
            commits[repo] = repo_commits
    return effective_start, commits
