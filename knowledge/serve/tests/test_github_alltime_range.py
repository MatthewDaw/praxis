"""Unit tests for range=alltime commit-activity fetch (R9).

Covers the ticket's acceptance condition: given range=alltime, exactly ceil(months_span/12)
discovery queries are issued plus at most one history query per active repository, and
effective_start is no earlier than the later of the account creation date and now minus five
years — no unbounded historical scan is ever issued.
"""

from __future__ import annotations

import math
from datetime import date

from knowledge.serve.github_commits import (
    alltime_chunks,
    fetch_commit_activity_alltime,
    months_span,
    resolve_alltime_start,
)


class _FakeTransport:
    """Records every call and dispatches a canned response by query shape."""

    def __init__(self, discovery_repos: list[str], history_by_repo: dict[str, list[dict]]):
        self.discovery_repos = discovery_repos
        self.history_by_repo = history_by_repo
        self.discovery_calls: list[dict] = []
        self.history_calls: list[dict] = []

    def __call__(self, query: str, variables: dict, token: str | None) -> dict:
        if "contributionsCollection" in query:
            self.discovery_calls.append(variables)
            return {
                "data": {
                    "user": {
                        "contributionsCollection": {
                            "commitContributionsByRepository": [
                                {"repository": {"nameWithOwner": name}} for name in self.discovery_repos
                            ]
                        }
                    }
                }
            }
        self.history_calls.append(variables)
        owner, name = variables["owner"], variables["name"]
        commits = self.history_by_repo.get(f"{owner}/{name}", [])
        return {
            "data": {
                "repository": {
                    "defaultBranchRef": {
                        "target": {
                            "history": {
                                "nodes": [
                                    {
                                        "additions": c["additions"],
                                        "deletions": c["deletions"],
                                        "committedDate": c["committedDate"],
                                        "author": {"user": {"login": c["author_login"]}},
                                    }
                                    for c in commits
                                ]
                            }
                        }
                    }
                }
            }
        }


def test_account_older_than_lookback_floors_at_now_minus_five_years():
    account_created_at = date(2010, 3, 1)
    now = date(2026, 7, 25)
    effective_start = resolve_alltime_start(account_created_at, now)
    assert effective_start == date(2021, 7, 25)
    assert effective_start >= date(now.year - 5, now.month, now.day)


def test_account_newer_than_lookback_floors_at_account_creation():
    account_created_at = date(2024, 1, 15)
    now = date(2026, 7, 25)
    effective_start = resolve_alltime_start(account_created_at, now)
    assert effective_start == account_created_at


def test_discovery_query_count_matches_ceil_months_span_over_twelve():
    start, end = date(2021, 7, 25), date(2026, 7, 25)
    chunks = alltime_chunks(start, end)
    expected = math.ceil(months_span(start, end) / 12)
    assert len(chunks) == expected == 6


def test_fetch_commit_activity_alltime_issues_exact_discovery_and_history_counts():
    account_created_at = date(2010, 3, 1)  # older than the 5-year lookback -> floored
    now = date(2026, 7, 25)
    effective_start = resolve_alltime_start(account_created_at, now)
    expected_discovery = math.ceil(months_span(effective_start, now) / 12)

    transport = _FakeTransport(
        discovery_repos=["acme/one", "acme/two"],
        history_by_repo={
            "acme/one": [
                {"additions": 1, "deletions": 0, "committedDate": "2022-01-01T00:00:00Z", "author_login": "mattdaw7"}
            ],
            "acme/two": [],
        },
    )

    got_effective_start, result = fetch_commit_activity_alltime(
        "mattdaw7", account_created_at, now, transport=transport
    )

    assert got_effective_start == effective_start
    assert got_effective_start >= date(now.year - 5, now.month, now.day)
    assert len(transport.discovery_calls) == expected_discovery
    # At most one history query per active repository (two active repos discovered).
    assert len(transport.history_calls) == 2
    assert {v["owner"] + "/" + v["name"] for v in transport.history_calls} == {"acme/one", "acme/two"}
    assert set(result.keys()) == {"acme/one", "acme/two"}


def test_fetch_commit_activity_alltime_no_active_repos_issues_no_history_queries():
    account_created_at = date(2023, 1, 1)
    now = date(2026, 7, 25)
    transport = _FakeTransport(discovery_repos=[], history_by_repo={})

    effective_start, result = fetch_commit_activity_alltime(
        "mattdaw7", account_created_at, now, transport=transport
    )

    assert effective_start == account_created_at
    assert len(transport.discovery_calls) == math.ceil(months_span(account_created_at, now) / 12)
    assert len(transport.history_calls) == 0
    assert result == {}
