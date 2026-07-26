"""Unit tests for GET /productivity's GitHub key-status handling (R21).

Acceptance condition: given the backend reports a key status of missing, expired or
insufficient_scope, the response carries `{"key_status": ...}` naming exactly which of
those three applies -- never a raw 401/403 exception, and never a normal series payload
(so the frontend panel, not tested here, can render the matching operator message).

Pure unit tests against `build_series`/`get_series_cached` with a monkeypatched token
resolver and commit-activity fetch -- no Postgres needed, because an auth failure is
discovered before `s4_series`'s DB call is ever reached (see `productivity_route.build_series`).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from knowledge.serve import github_commits, github_token, productivity_route


@pytest.fixture(autouse=True)
def _reset_token_cache():
    github_token.invalidate_github_token()
    yield
    github_token.invalidate_github_token()


def test_missing_token_returns_key_status_missing_without_calling_github(monkeypatch):
    monkeypatch.setattr(github_token, "resolve_github_token", lambda: None)

    called = {"n": 0}

    def _boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("must never call GitHub when the token is missing")

    monkeypatch.setattr(github_commits, "fetch_commit_activity", _boom)

    result = productivity_route.build_series(conn=None, org_id="org-1", range_="week")

    assert result == {"key_status": productivity_route.KEY_STATUS_MISSING}
    assert called["n"] == 0


def test_expired_token_returns_key_status_expired_and_invalidates_cache(monkeypatch):
    monkeypatch.setattr(github_token, "resolve_github_token", lambda: "gh" + "p_faketoken1234567890")

    invalidated = {"n": 0}
    monkeypatch.setattr(
        github_token, "invalidate_github_token", lambda: invalidated.__setitem__("n", invalidated["n"] + 1)
    )

    def _raise_expired(*args, **kwargs):
        raise github_commits.GitHubAuthExpired("401")

    monkeypatch.setattr(github_commits, "fetch_commit_activity", _raise_expired)

    result = productivity_route.build_series(conn=None, org_id="org-1", range_="week")

    assert result == {"key_status": productivity_route.KEY_STATUS_EXPIRED}
    assert invalidated["n"] == 1  # so the next call re-fetches a rotated secret


def test_insufficient_scope_returns_key_status_insufficient_scope(monkeypatch):
    monkeypatch.setattr(github_token, "resolve_github_token", lambda: "gh" + "p_faketoken1234567890")

    def _raise_scope(*args, **kwargs):
        raise github_commits.GitHubInsufficientScope("403")

    monkeypatch.setattr(github_commits, "fetch_commit_activity", _raise_scope)

    result = productivity_route.build_series(conn=None, org_id="org-1", range_="week")

    assert result == {"key_status": productivity_route.KEY_STATUS_INSUFFICIENT_SCOPE}


def test_each_key_status_is_distinct():
    statuses = {
        productivity_route.KEY_STATUS_MISSING,
        productivity_route.KEY_STATUS_EXPIRED,
        productivity_route.KEY_STATUS_INSUFFICIENT_SCOPE,
    }
    assert len(statuses) == 3


def test_key_status_result_is_never_cached(monkeypatch):
    """A key-status error must never be served from cache -- an operator fixing the
    token needs the very next request to try GitHub again, not wait out a TTL band."""
    monkeypatch.setattr(github_token, "resolve_github_token", lambda: None)

    calls = {"n": 0}

    def _build_series(conn, org_id, range_, *, now=None):
        calls["n"] += 1
        return {"key_status": productivity_route.KEY_STATUS_MISSING}

    monkeypatch.setattr(productivity_route, "build_series", _build_series)

    now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    first = productivity_route.get_series_cached(None, "org-1", "user-1", "week", now=now)
    second = productivity_route.get_series_cached(None, "org-1", "user-1", "week", now=now)

    assert first == {"key_status": productivity_route.KEY_STATUS_MISSING}
    assert second == {"key_status": productivity_route.KEY_STATUS_MISSING}
    assert calls["n"] == 2  # never satisfied from cache
