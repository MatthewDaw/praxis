"""Unit tests for the GitHub GraphQL commit-activity client (R2/R3).

Covers the ticket's acceptance condition: given a date window, the client returns per-repo
commit nodes each carrying ``additions``, ``deletions``, ``committedDate`` and ``author_login``,
issuing exactly one discovery query per year-chunk plus one history query per active repository.
"""

from __future__ import annotations

from datetime import date

from knowledge.serve.github_commits import fetch_commit_activity, year_chunks


def _discovery_response(repo_names: list[str]) -> dict:
    return {
        "data": {
            "user": {
                "contributionsCollection": {
                    "commitContributionsByRepository": [
                        {"repository": {"nameWithOwner": name}} for name in repo_names
                    ]
                }
            }
        }
    }


def _history_response(commits: list[dict]) -> dict:
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


class _FakeTransport:
    """Records every call and dispatches a canned response by query shape."""

    def __init__(self, discovery_by_window: dict[tuple[str, str], list[str]], history_by_repo: dict[str, list[dict]]):
        self.discovery_by_window = discovery_by_window
        self.history_by_repo = history_by_repo
        self.discovery_calls: list[dict] = []
        self.history_calls: list[dict] = []

    def __call__(self, query: str, variables: dict, token) -> dict:
        if "contributionsCollection" in query:
            self.discovery_calls.append(variables)
            key = (variables["from"], variables["to"])
            return _discovery_response(self.discovery_by_window[key])
        assert "history(" in query
        self.history_calls.append(variables)
        repo = f"{variables['owner']}/{variables['name']}"
        return _history_response(self.history_by_repo[repo])


def test_year_chunks_splits_multi_year_window_at_calendar_boundaries():
    chunks = year_chunks(date(2023, 6, 1), date(2024, 3, 1))
    assert chunks == [
        (date(2023, 6, 1), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2024, 3, 1)),
    ]


def test_year_chunks_single_calendar_year_is_one_chunk():
    assert year_chunks(date(2024, 1, 1), date(2024, 6, 30)) == [(date(2024, 1, 1), date(2024, 6, 30))]


def test_fetch_commit_activity_issues_one_discovery_query_per_year_chunk_and_one_history_query_per_active_repo():
    start, end = date(2023, 6, 1), date(2024, 3, 1)
    chunks = year_chunks(start, end)
    win1 = (chunks[0][0].isoformat() + "T00:00:00Z", chunks[0][1].isoformat() + "T00:00:00Z")
    win2 = (chunks[1][0].isoformat() + "T00:00:00Z", chunks[1][1].isoformat() + "T00:00:00Z")

    transport = _FakeTransport(
        discovery_by_window={
            win1: ["acme/one", "acme/two"],
            win2: ["acme/two", "acme/three"],
        },
        history_by_repo={
            "acme/one": [
                {"additions": 10, "deletions": 2, "committedDate": "2023-07-01T00:00:00Z", "author_login": "mattdaw7"}
            ],
            "acme/two": [
                {"additions": 5, "deletions": 1, "committedDate": "2023-08-01T00:00:00Z", "author_login": "mattdaw7"},
                {"additions": 3, "deletions": 0, "committedDate": "2024-02-01T00:00:00Z", "author_login": "mattdaw7"},
            ],
            "acme/three": [
                {"additions": 20, "deletions": 4, "committedDate": "2024-02-15T00:00:00Z", "author_login": "mattdaw7"}
            ],
        },
    )

    result = fetch_commit_activity("mattdaw7", start, end, transport=transport)

    # One discovery query per year-chunk (2 chunks in this window) — not per repo, not per day.
    assert len(transport.discovery_calls) == len(chunks) == 2
    # One history query per repo discovered active in ANY chunk (union, deduped): one, two, three.
    assert len(transport.history_calls) == 3
    assert {c["owner"] + "/" + c["name"] for c in transport.history_calls} == {
        "acme/one",
        "acme/two",
        "acme/three",
    }

    # The client returns per-repo commit nodes, each carrying the four required fields, plus a
    # truncated/reason envelope (see test_github_reliability.py) — untouched (False/None) here since
    # every call succeeded.
    repositories = result["repositories"]
    assert result["truncated"] is False
    assert result["reason"] is None
    assert set(repositories.keys()) == {"acme/one", "acme/two", "acme/three"}
    assert repositories["acme/two"] == [
        {"additions": 5, "deletions": 1, "committedDate": "2023-08-01T00:00:00Z", "author_login": "mattdaw7"},
        {"additions": 3, "deletions": 0, "committedDate": "2024-02-01T00:00:00Z", "author_login": "mattdaw7"},
    ]
    for commits in repositories.values():
        for node in commits:
            assert {"additions", "deletions", "committedDate", "author_login"} <= set(node)


def test_fetch_commit_activity_no_active_repos_issues_no_history_queries():
    start, end = date(2024, 1, 1), date(2024, 6, 30)
    transport = _FakeTransport(
        discovery_by_window={(start.isoformat() + "T00:00:00Z", end.isoformat() + "T00:00:00Z"): []},
        history_by_repo={},
    )

    result = fetch_commit_activity("mattdaw7", start, end, transport=transport)

    assert len(transport.discovery_calls) == 1
    assert len(transport.history_calls) == 0
    assert result["repositories"] == {}
    assert result["truncated"] is False
    assert result["reason"] is None
