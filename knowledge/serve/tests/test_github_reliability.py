"""Unit tests for GitHub GraphQL call reliability (R37).

Every GitHub call carries a bounded timeout with retry and exponential backoff that honors a
``Retry-After`` header, and a GitHub timeout, 5xx, secondary rate limit or a partial GraphQL
errors payload is surfaced through the same truncation-and-reason channel rather than being
silently reported as zero activity.

Acceptance condition: for each of a timeout, a 502, a secondary rate limit with ``Retry-After``,
and a payload carrying both ``data`` and ``errors``, :func:`fetch_commit_activity` returns
``truncated=True`` and ``reason`` equal to the enumerated constant for that case, and never
reports the affected window as a confident zero.
"""

from __future__ import annotations

from datetime import date

import pytest

from knowledge.serve.github_commits import (
    GitHubRateLimited,
    GitHubTimeout,
    GitHubUpstreamError,
    TruncationReason,
    fetch_commit_activity,
)

START, END = date(2024, 1, 1), date(2024, 6, 30)


def _discovery_ok(repo_names: list[str]) -> dict:
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


def _history_ok(commits: list[dict]) -> dict:
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


class _RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def test_genuine_no_active_repos_is_not_truncated():
    """The pre-existing "no activity" case stays truncated=False (a real, confident zero)."""

    def transport(query, variables, token):
        assert "contributionsCollection" in query
        return _discovery_ok([])

    result = fetch_commit_activity("mattdaw7", START, END, transport=transport, sleep=lambda s: None)

    assert result["repositories"] == {}
    assert result["truncated"] is False
    assert result["reason"] is None


def test_timeout_exhausting_retries_is_truncated_with_timeout_reason():
    calls = {"n": 0}

    def transport(query, variables, token):
        calls["n"] += 1
        raise GitHubTimeout("deadline exceeded")

    sleep = _RecordingSleep()
    result = fetch_commit_activity(
        "mattdaw7", START, END, transport=transport, sleep=sleep, max_retries=2
    )

    # Retried (not just one shot) before giving up.
    assert calls["n"] == 3  # initial attempt + 2 retries
    assert len(sleep.calls) == 2
    # Never a confident zero — flagged truncated with the timeout reason, not silently {}.
    assert result["truncated"] is True
    assert result["reason"] == TruncationReason.TIMEOUT
    assert result["repositories"] == {}


def test_upstream_502_is_truncated_with_upstream_reason():
    def transport(query, variables, token):
        raise GitHubUpstreamError(502)

    result = fetch_commit_activity(
        "mattdaw7", START, END, transport=transport, sleep=lambda s: None, max_retries=1
    )

    assert result["truncated"] is True
    assert result["reason"] == TruncationReason.UPSTREAM_ERROR


def test_secondary_rate_limit_honors_retry_after_then_truncates_if_still_limited():
    def transport(query, variables, token):
        raise GitHubRateLimited(retry_after=7.0)

    sleep = _RecordingSleep()
    result = fetch_commit_activity(
        "mattdaw7", START, END, transport=transport, sleep=sleep, max_retries=2
    )

    # Backoff honors the Retry-After value it was told (never sleeps for less than it).
    assert all(s >= 7.0 for s in sleep.calls)
    assert result["truncated"] is True
    assert result["reason"] == TruncationReason.RATE_LIMITED


def test_rate_limit_recovers_after_retry_is_not_truncated():
    calls = {"n": 0}

    def transport(query, variables, token):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GitHubRateLimited(retry_after=1.0)
        return _discovery_ok([])

    sleep = _RecordingSleep()
    result = fetch_commit_activity("mattdaw7", START, END, transport=transport, sleep=sleep)

    assert calls["n"] == 2
    assert sleep.calls == [1.0]
    assert result["truncated"] is False
    assert result["reason"] is None


def test_partial_graphql_errors_payload_is_truncated_but_keeps_partial_data():
    def transport(query, variables, token):
        if "contributionsCollection" in query:
            body = _discovery_ok(["acme/one"])
            body["errors"] = [{"message": "something partially failed"}]
            return body
        return _history_ok(
            [{"additions": 1, "deletions": 0, "committedDate": "2024-02-01T00:00:00Z", "author_login": "x"}]
        )

    result = fetch_commit_activity("mattdaw7", START, END, transport=transport, sleep=lambda s: None)

    assert result["truncated"] is True
    assert result["reason"] == TruncationReason.PARTIAL_ERRORS
    # Partial data is preserved, not discarded/zeroed just because errors were also present.
    assert result["repositories"] == {
        "acme/one": [
            {"additions": 1, "deletions": 0, "committedDate": "2024-02-01T00:00:00Z", "author_login": "x"}
        ]
    }


def test_history_failure_for_one_repo_does_not_zero_the_whole_result():
    def transport(query, variables, token):
        if "contributionsCollection" in query:
            return _discovery_ok(["acme/one", "acme/two"])
        if variables["name"] == "one":
            raise GitHubUpstreamError(502)
        return _history_ok(
            [{"additions": 2, "deletions": 1, "committedDate": "2024-03-01T00:00:00Z", "author_login": "x"}]
        )

    result = fetch_commit_activity(
        "mattdaw7", START, END, transport=transport, sleep=lambda s: None, max_retries=1
    )

    assert result["truncated"] is True
    assert result["reason"] == TruncationReason.UPSTREAM_ERROR
    # acme/two succeeded and is present with real data (not omitted/zeroed by the acme/one failure).
    assert result["repositories"]["acme/two"] == [
        {"additions": 2, "deletions": 1, "committedDate": "2024-03-01T00:00:00Z", "author_login": "x"}
    ]
    # acme/one's outcome is unknown (not a confirmed zero) so it is never reported as [].
    assert "acme/one" not in result["repositories"]


@pytest.mark.parametrize("bad_max_retries", [-1])
def test_max_retries_is_bounded_non_negative(bad_max_retries):
    with pytest.raises(ValueError):
        fetch_commit_activity(
            "mattdaw7", START, END, transport=lambda *a: {}, max_retries=bad_max_retries
        )
