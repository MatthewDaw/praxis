"""GitHub GraphQL client for the productivity feature's commit-activity fetch (R2/R3).

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
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"

# (query, variables, token) -> parsed JSON response body (``{"data": {...}}``).
Transport = Callable[[str, dict[str, Any], Optional[str]], dict[str, Any]]

DISCOVERY_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      commitContributionsByRepository(maxRepositories: 100) {
        repository { nameWithOwner }
      }
    }
  }
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
}
"""


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


def _iso(d: date) -> str:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _default_transport(query: str, variables: dict[str, Any], token: Optional[str]) -> dict[str, Any]:
    import httpx

    headers = {"Authorization": f"bearer {token}"} if token else {}
    resp = httpx.post(
        GITHUB_GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers=headers,
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def _discover_active_repos(
    login: str,
    chunks: list[tuple[date, date]],
    transport: Transport,
    token: Optional[str],
) -> list[str]:
    """Issue one discovery query per year-chunk; return active repos (dedup, first-seen order)."""
    seen: dict[str, None] = {}
    for since, until in chunks:
        variables = {"login": login, "from": _iso(since), "to": _iso(until)}
        data = transport(DISCOVERY_QUERY, variables, token)
        contributions = (
            (data.get("data") or {}).get("user") or {}
        ).get("contributionsCollection") or {}
        entries = contributions.get("commitContributionsByRepository") or []
        for entry in entries:
            name = (entry.get("repository") or {}).get("nameWithOwner")
            if name and name not in seen:
                seen[name] = None
    return list(seen.keys())


def _fetch_repo_commits(
    repo: str,
    start: date,
    end: date,
    transport: Transport,
    token: Optional[str],
) -> list[dict[str, Any]]:
    """Issue one history query for ``repo``; return its commit nodes for ``[start, end]``."""
    owner, _, name = repo.partition("/")
    variables = {"owner": owner, "name": name, "since": _iso(start), "until": _iso(end)}
    data = transport(HISTORY_QUERY, variables, token)
    repo_data = (data.get("data") or {}).get("repository") or {}
    target = (repo_data.get("defaultBranchRef") or {}).get("target") or {}
    nodes = (target.get("history") or {}).get("nodes") or []
    return [
        {
            "additions": node.get("additions"),
            "deletions": node.get("deletions"),
            "committedDate": node.get("committedDate"),
            "author_login": ((node.get("author") or {}).get("user") or {}).get("login"),
        }
        for node in nodes
    ]


def fetch_commit_activity(
    login: str,
    start: date,
    end: date,
    *,
    token: Optional[str] = None,
    transport: Optional[Transport] = None,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch per-repository commit activity for ``login`` over the calendar-date window ``[start, end]``.

    Issues exactly one discovery query per calendar-year chunk of the window (to find which
    repositories ``login`` committed to), then exactly one history query per repository found
    active in any chunk (to pull its commits for the full window). Returns a dict keyed by
    ``"owner/name"`` -> a list of commit dicts, each carrying ``additions``, ``deletions``,
    ``committedDate`` and ``author_login``.

    ``token`` authenticates the outbound GraphQL request (the caller resolves/rotates it; this
    module never caches or logs it). ``transport`` is injectable (query, variables, token) ->
    parsed JSON body, defaulting to a real ``httpx`` POST against the GitHub GraphQL API.
    """
    xport: Transport = transport or _default_transport
    chunks = year_chunks(start, end)
    active_repos = _discover_active_repos(login, chunks, xport, token)
    return {repo: _fetch_repo_commits(repo, start, end, xport, token) for repo in active_repos}
