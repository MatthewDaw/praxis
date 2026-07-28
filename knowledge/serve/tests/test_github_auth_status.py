"""Unit tests for the GitHub token-rejection classification (R21).

The productivity feature's GitHub client must distinguish a token that GitHub outright
rejects (401 -- invalid/revoked/expired) from one that's valid but lacks the required
permission (403, not a secondary rate limit -- those carry ``Retry-After`` and are a
different, retryable failure per R37). Neither is ever retried (retrying a dead token
can't succeed) and neither is folded into ``truncated``/``reason`` -- both raise
immediately so the productivity route can turn them into an operator-facing key status
instead of ever letting a raw 401/403 escape.
"""

from __future__ import annotations

from datetime import date

import pytest

from knowledge.serve.github_commits import (
    GitHubAuthExpired,
    GitHubInsufficientScope,
    GitHubRateLimited,
    fetch_commit_activity,
)

START, END = date(2024, 1, 1), date(2024, 6, 30)


def test_401_raises_auth_expired_immediately_never_retried():
    calls = {"n": 0}

    def transport(query, variables, token):
        calls["n"] += 1
        raise GitHubAuthExpired("401")

    with pytest.raises(GitHubAuthExpired):
        fetch_commit_activity(
            ["acme/one"], START, END, transport=transport, sleep=lambda s: None, max_retries=3
        )

    assert calls["n"] == 1  # never retried -- a dead token can't be fixed by trying again


def test_plain_403_raises_insufficient_scope_immediately_never_retried():
    calls = {"n": 0}

    def transport(query, variables, token):
        calls["n"] += 1
        raise GitHubInsufficientScope("403")

    with pytest.raises(GitHubInsufficientScope):
        fetch_commit_activity(
            ["acme/one"], START, END, transport=transport, sleep=lambda s: None, max_retries=3
        )

    assert calls["n"] == 1


def test_auth_errors_are_distinct_from_rate_limited_403():
    """A 403 carrying Retry-After is a RATE LIMIT (retryable), never an auth failure."""
    assert not issubclass(GitHubRateLimited, GitHubInsufficientScope)
    assert not issubclass(GitHubInsufficientScope, GitHubRateLimited)


def test_auth_errors_are_not_transport_errors():
    """Auth errors must never be silently caught by the TruncationReason retry/backoff
    path meant for transient network/rate-limit failures."""
    from knowledge.serve.github_commits import GitHubTransportError

    assert not issubclass(GitHubAuthExpired, GitHubTransportError)
    assert not issubclass(GitHubInsufficientScope, GitHubTransportError)
