"""Unit tests for range=alltime commit-activity fetch (R9).

Covers the ticket's acceptance condition: given range=alltime, exactly one history query is
issued per repository in the caller-supplied repo list (no discovery step), and effective_start
is no earlier than the later of the account creation date and now minus five years — no
unbounded historical scan is ever issued.
"""

from __future__ import annotations

from datetime import date

from knowledge.serve.github_commits import (
    fetch_commit_activity_alltime,
    resolve_alltime_start,
)


class _FakeTransport:
    """Records every history call and dispatches a canned response per repo."""

    def __init__(self, history_by_repo: dict[str, list[dict]]):
        self.history_by_repo = history_by_repo
        self.history_calls: list[dict] = []

    def __call__(self, query: str, variables: dict, token: str | None) -> dict:
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


def test_fetch_commit_activity_alltime_issues_exactly_one_history_query_per_repo():
    account_created_at = date(2010, 3, 1)  # older than the 5-year lookback -> floored
    now = date(2026, 7, 25)
    effective_start = resolve_alltime_start(account_created_at, now)
    repos = ["acme/one", "acme/two"]

    transport = _FakeTransport(
        history_by_repo={
            "acme/one": [
                {"additions": 1, "deletions": 0, "committedDate": "2022-01-01T00:00:00Z", "author_login": "mattdaw7"}
            ],
            "acme/two": [],
        },
    )

    got_effective_start, result = fetch_commit_activity_alltime(
        repos, account_created_at, now, transport=transport
    )

    assert got_effective_start == effective_start
    assert got_effective_start >= date(now.year - 5, now.month, now.day)
    # Exactly one history query per repo -- no discovery query at all.
    assert len(transport.history_calls) == 2
    assert {v["owner"] + "/" + v["name"] for v in transport.history_calls} == {"acme/one", "acme/two"}
    # Every history query spans the full floored window.
    for v in transport.history_calls:
        assert v["since"] == effective_start.isoformat() + "T00:00:00Z"
        assert v["until"] == now.isoformat() + "T00:00:00Z"
    assert set(result.keys()) == {"acme/one", "acme/two"}


def test_fetch_commit_activity_alltime_empty_repo_list_issues_no_history_queries():
    account_created_at = date(2023, 1, 1)
    now = date(2026, 7, 25)
    transport = _FakeTransport(history_by_repo={})

    effective_start, result = fetch_commit_activity_alltime(
        [], account_created_at, now, transport=transport
    )

    assert effective_start == account_created_at
    assert len(transport.history_calls) == 0
    assert result == {}


def test_fetch_commit_activity_alltime_dedupes_repeated_repo_entries():
    account_created_at = date(2023, 1, 1)
    now = date(2026, 7, 25)
    transport = _FakeTransport(
        history_by_repo={
            "acme/one": [
                {"additions": 1, "deletions": 0, "committedDate": "2024-01-01T00:00:00Z", "author_login": "x"}
            ],
        },
    )

    _effective_start, result = fetch_commit_activity_alltime(
        ["acme/one", "acme/one"], account_created_at, now, transport=transport
    )

    assert len(transport.history_calls) == 1
    assert set(result.keys()) == {"acme/one"}
