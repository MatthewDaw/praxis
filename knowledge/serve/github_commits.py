"""GitHub GraphQL client for the productivity feature's commit-activity fetch (R2/R3/R37).

Given a date window and an EXPLICIT, caller-supplied list of repositories (``"owner/name"``
pairs — see ``productivity_route.tracked_repos()``), returns per-repository commit activity —
each commit node carrying ``additions``, ``deletions``, ``committedDate`` and the author's
``login`` — by issuing exactly one *history* GraphQL query per repository in that list to pull
its commits for the whole window.

Repository discovery previously ran via a GraphQL ``contributionsCollection`` query against the
account, but that field silently omits PRIVATE repositories owned by a GitHub ORGANIZATION the
account is merely a member of (confirmed against the live API — this is not the "hide private
contributions" profile-privacy setting, which is a separate, correctly-zero field). Per-repo
history fetching (the ``HISTORY_QUERY`` below) has no such gap, so discovery was replaced with a
static, explicitly configured repo list instead.

The caller supplies the GitHub token (this module never resolves or caches one itself, and never
logs it, writes it to the graph, or puts it in a return value) and the date window as plain
calendar dates — this module never reads a client-supplied timezone/offset (bucket boundaries
belong to the caller, not this client).

RELIABILITY (R37): every call is bounded by a timeout and retried with exponential backoff. A
GitHub timeout, upstream 5xx, secondary rate limit (honoring ``Retry-After``), or a partial
GraphQL ``errors`` payload never gets silently reported as zero activity for the affected window —
:func:`fetch_commit_activity` instead returns ``truncated=True`` with a :class:`TruncationReason`
constant naming the cause, keeping whatever partial data was already fetched.

A rejection of the TOKEN ITSELF (GitHub 401/403) is a different kind of failure and is never
retried or folded into ``truncated``: it raises :class:`GitHubAuthExpired` or
:class:`GitHubInsufficientScope` immediately, for the caller (the productivity route, R21) to
turn into an operator-facing key status instead of a raw HTTP error.
"""

from __future__ import annotations

import calendar
import time
from datetime import date, datetime, timezone
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


class GitHubAuthError(Exception):
    """The token itself was rejected by GitHub — never retried (see subclasses).

    Deliberately NOT a :class:`GitHubTransportError`: retrying a bad token can't
    succeed, and folding it into ``TruncationReason`` would make a dead key
    indistinguishable from a transient network/rate-limit blip the caller can
    recover from just by trying again. The productivity route (R21) catches
    these directly to report an operator-facing key status instead.
    """


class GitHubAuthExpired(GitHubAuthError):
    """GitHub returned 401 — the token is absent, invalid, or revoked/expired."""


class GitHubInsufficientScope(GitHubAuthError):
    """GitHub returned 403 (not a secondary rate limit, which carries ``Retry-After``
    and is handled by :class:`GitHubRateLimited`) — the token is valid but lacks the
    permission the query needs (e.g. missing ``Contents: Read``)."""


_REASON_FOR_ERROR: dict[type[GitHubTransportError], str] = {
    GitHubTimeout: TruncationReason.TIMEOUT,
    GitHubUpstreamError: TruncationReason.UPSTREAM_ERROR,
    GitHubRateLimited: TruncationReason.RATE_LIMITED,
}

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

    if resp.status_code == 401:
        raise GitHubAuthExpired(f"GitHub rejected the token: {resp.status_code}")
    if resp.status_code in (403, 429) and "retry-after" in resp.headers:
        raise GitHubRateLimited(retry_after=float(resp.headers["retry-after"]))
    if resp.status_code == 403:
        raise GitHubInsufficientScope(f"GitHub token lacks required scope: {resp.status_code}")
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


def _dedupe(repos: list[str]) -> list[str]:
    """``repos`` deduped in first-seen order (a caller-supplied list may repeat an entry)."""
    seen: dict[str, None] = {}
    for repo in repos:
        if repo not in seen:
            seen[repo] = None
    return list(seen.keys())


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
    repos: list[str],
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
    """Fetch per-repository commit activity for ``repos`` over the calendar-date window ``[start, end]``.

    Issues exactly one history query per repository in ``repos`` (an explicit, caller-supplied
    list of ``"owner/name"`` pairs — see ``productivity_route.tracked_repos()`` — deduped in
    first-seen order) to pull its commits for the full window. There is no discovery step: which
    repos to query is config, not something inferred per request.

    Returns ``{"repositories": {...}, "truncated": bool, "reason": str|None, "points_spent": int}``:
    ``repositories`` is keyed by ``"owner/name"`` -> a list of commit dicts (``additions``,
    ``deletions``, ``committedDate``, ``author_login``). ``truncated``/``reason`` surface a
    GitHub timeout, upstream 5xx, secondary rate limit, or partial GraphQL ``errors`` payload
    (see :class:`TruncationReason`) that survived ``max_retries`` retries with exponential
    backoff — the affected repo is OMITTED from ``repositories`` rather than reported as a
    confirmed zero, and any repo that succeeded is still included with real data.
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

    reason: Optional[str] = None
    points = 0
    repositories: dict[str, list[dict[str, Any]]] = {}
    for repo in _dedupe(repos):
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
    repos: list[str],
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
    ``now`` minus ``max_lookback_years``. Issues exactly one history query per repository in
    ``repos`` (the same explicit, caller-supplied list :func:`fetch_commit_activity` takes) for
    the ``[effective_start, now]`` window.

    Like :func:`fetch_commit_activity`, every call is bounded/retried (R37) and a repo whose call
    ultimately fails is simply omitted rather than reported as a confirmed zero.

    Returns ``(effective_start, commit_activity)`` so the caller/UI can label the floored window.
    """
    xport: Transport = transport or _default_transport
    retry_kwargs = dict(max_retries=max_retries, base_delay=base_delay, max_delay=max_delay, sleep=sleep)
    effective_start = resolve_alltime_start(account_created_at, now, max_lookback_years)

    commits: dict[str, list[dict[str, Any]]] = {}
    for repo in _dedupe(repos):
        repo_commits, _repo_reason, _repo_points = _fetch_repo_commits(
            repo, effective_start, now, xport, token, **retry_kwargs
        )
        if repo_commits is not None:
            commits[repo] = repo_commits
    return effective_start, commits
