"""Unit tests for the GitHub GraphQL commit-activity client (R2/R3).

Covers the ticket's acceptance condition: given a date window and an explicit list of
``"owner/name"`` repos, the client returns per-repo commit nodes each carrying ``additions``,
``deletions``, ``committedDate`` and ``author_login``, issuing exactly one history query per
repo in that list -- there is no discovery step (see ``github_commits`` module docstring for
why GraphQL-based discovery was replaced with a static, caller-supplied repo list).
"""

from __future__ import annotations

from datetime import date

from knowledge.serve.github_commits import fetch_commit_activity


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
    """Records every history call and dispatches a canned response per repo."""

    def __init__(self, history_by_repo: dict[str, list[dict]]):
        self.history_by_repo = history_by_repo
        self.history_calls: list[dict] = []

    def __call__(self, query: str, variables: dict, token) -> dict:
        assert "history(" in query
        self.history_calls.append(variables)
        repo = f"{variables['owner']}/{variables['name']}"
        return _history_response(self.history_by_repo[repo])


def test_fetch_commit_activity_issues_exactly_one_history_query_per_configured_repo():
    start, end = date(2023, 6, 1), date(2024, 3, 1)
    repos = ["acme/one", "acme/two", "acme/three"]

    transport = _FakeTransport(
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

    result = fetch_commit_activity(repos, start, end, transport=transport)

    # One history query per configured repo -- no discovery query at all.
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


def test_fetch_commit_activity_empty_repo_list_issues_no_history_queries():
    start, end = date(2024, 1, 1), date(2024, 6, 30)
    transport = _FakeTransport(history_by_repo={})

    result = fetch_commit_activity([], start, end, transport=transport)

    assert len(transport.history_calls) == 0
    assert result["repositories"] == {}
    assert result["truncated"] is False
    assert result["reason"] is None


def test_fetch_commit_activity_dedupes_repeated_repo_entries():
    start, end = date(2024, 1, 1), date(2024, 6, 30)
    transport = _FakeTransport(
        history_by_repo={
            "acme/one": [
                {"additions": 1, "deletions": 0, "committedDate": "2024-02-01T00:00:00Z", "author_login": "x"}
            ],
        },
    )

    result = fetch_commit_activity(["acme/one", "acme/one"], start, end, transport=transport)

    assert len(transport.history_calls) == 1
    assert set(result["repositories"].keys()) == {"acme/one"}
