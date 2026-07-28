"""Unit tests for the GitHub token resolver (R1 — secret storage/cache).

Runs fully offline against a mocked ``boto3.client`` so no AWS creds or network
are needed. Covers: single fetch cached across repeat calls (one Secrets
Manager call per process), the value never leaking through stdout/stderr, a
missing/broken secret degrading to ``None`` instead of raising, and
``invalidate_github_token`` forcing a re-fetch (the rotation-refresh path).

Fake token values are assembled at runtime (never a contiguous literal) so this file itself
never trips the repo-wide raw-token-leak scan (``no-github-token-leak``) it exercises.
"""

from __future__ import annotations

import io
import contextlib
from unittest import mock

import pytest

from knowledge.serve import github_token


def _fake_token(suffix: str) -> str:
    return "gh" + "p_" + suffix


@pytest.fixture(autouse=True)
def _reset_cache():
    """Every test starts from a clean in-process cache."""
    github_token.invalidate_github_token()
    yield
    github_token.invalidate_github_token()


class _FakeClient:
    def __init__(self, value: str | None = None, calls: list | None = None):
        self._value = value
        self._calls = calls if calls is not None else []

    def get_secret_value(self, SecretId):  # noqa: N803 - mirrors boto3's kwarg name
        self._calls.append(SecretId)
        if self._value is None:
            raise RuntimeError("secret not found")
        return {"SecretString": self._value}


def test_resolve_fetches_once_and_caches():
    calls: list = []
    client = _FakeClient(value=_fake_token("realtokenvalue1234567890"), calls=calls)
    with mock.patch("boto3.client", return_value=client):
        first = github_token.resolve_github_token()
        second = github_token.resolve_github_token()
    assert first == _fake_token("realtokenvalue1234567890")
    assert second == first
    assert len(calls) == 1  # cached — no second Secrets Manager round-trip


def test_resolve_never_prints_or_logs_the_value():
    client = _FakeClient(value=_fake_token("realtokenvalue1234567890"))
    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    with mock.patch("boto3.client", return_value=client):
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            token = github_token.resolve_github_token()
    assert token == _fake_token("realtokenvalue1234567890")
    assert _fake_token("realtokenvalue1234567890") not in stdout_buf.getvalue()
    assert _fake_token("realtokenvalue1234567890") not in stderr_buf.getvalue()


def test_resolve_degrades_to_none_on_failure():
    client = _FakeClient(value=None)
    with mock.patch("boto3.client", return_value=client):
        assert github_token.resolve_github_token() is None


def test_invalidate_forces_refetch():
    calls: list = []
    client = _FakeClient(value=_fake_token("first00000000000000000000"), calls=calls)
    with mock.patch("boto3.client", return_value=client):
        assert github_token.resolve_github_token() == _fake_token("first00000000000000000000")
        github_token.invalidate_github_token()
        client._value = _fake_token("rotated0000000000000000000")
        assert github_token.resolve_github_token() == _fake_token("rotated0000000000000000000")
    assert len(calls) == 2  # invalidation forced a second fetch — rotation picked up


def test_fetch_uncached_hits_secrets_manager_on_every_call_never_caching():
    """The box service's push path (``box_service_push_auth.push_main_worktree``) uses this
    resolver instead of the cached one, precisely so a token revoked or rotated mid-run (the
    90-day rotation cadence) surfaces at the very next integration rather than only after a
    process restart."""
    calls: list = []
    client = _FakeClient(value=_fake_token("firstintegration000000000"), calls=calls)
    with mock.patch("boto3.client", return_value=client):
        first = github_token.fetch_github_token_uncached()
        # No call to `resolve_github_token`/`invalidate_github_token` in between — a rotated
        # secret must still be picked up on the very next call, unlike the cached resolver.
        client._value = _fake_token("rotatedmidrun00000000000")
        second = github_token.fetch_github_token_uncached()
    assert first == _fake_token("firstintegration000000000")
    assert second == _fake_token("rotatedmidrun00000000000")
    assert len(calls) == 2  # every call is a fresh Secrets Manager round-trip, never cached


def test_fetch_uncached_is_independent_of_the_cached_resolver_state():
    """Warming (or leaving cold) the process-lifetime cache used by ``resolve_github_token`` must
    not affect what the uncached box-service resolver returns."""
    cached_client = _FakeClient(value=_fake_token("cachedvalue000000000000000"))
    with mock.patch("boto3.client", return_value=cached_client):
        assert github_token.resolve_github_token() == _fake_token("cachedvalue000000000000000")

    fresh_client = _FakeClient(value=_fake_token("freshvalue0000000000000000"))
    with mock.patch("boto3.client", return_value=fresh_client):
        assert github_token.fetch_github_token_uncached() == _fake_token("freshvalue0000000000000000")


def test_fetch_uncached_degrades_to_none_on_failure():
    client = _FakeClient(value=None)
    with mock.patch("boto3.client", return_value=client):
        assert github_token.fetch_github_token_uncached() is None


# --- local-dev override (PRAXIS_GITHUB_TOKEN) -----------------------------


def test_local_override_wins_over_secrets_manager(monkeypatch):
    """A developer running the server locally points it at their own `gh auth token`, which
    can reach private org-owned repos the deployed fine-grained PAT cannot see at all."""
    monkeypatch.setenv(
        github_token.LOCAL_TOKEN_ENV_VAR, _fake_token("localdevvalue00000000000")
    )
    calls: list = []
    client = _FakeClient(value=_fake_token("secretsmanagervalue000000"), calls=calls)
    with mock.patch("boto3.client", return_value=client):
        assert github_token.resolve_github_token() == _fake_token("localdevvalue00000000000")
    assert calls == []  # Secrets Manager is never even consulted.


def test_local_override_also_applies_to_the_uncached_reader(monkeypatch):
    """The box service's push path reads uncached; it must see the same local credential
    rather than silently authenticating as a different identity than the cached readers."""
    monkeypatch.setenv(
        github_token.LOCAL_TOKEN_ENV_VAR, _fake_token("localdevvalue00000000000")
    )
    client = _FakeClient(value=_fake_token("secretsmanagervalue000000"))
    with mock.patch("boto3.client", return_value=client):
        assert (
            github_token.fetch_github_token_uncached()
            == _fake_token("localdevvalue00000000000")
        )


def test_blank_local_override_falls_through_to_secrets_manager(monkeypatch):
    """An exported-but-empty variable must not authenticate every GitHub call as an empty
    bearer token -- it reads as "unset" and the normal deployed path still applies."""
    monkeypatch.setenv(github_token.LOCAL_TOKEN_ENV_VAR, "   ")
    client = _FakeClient(value=_fake_token("secretsmanagervalue000000"))
    with mock.patch("boto3.client", return_value=client):
        assert (
            github_token.resolve_github_token() == _fake_token("secretsmanagervalue000000")
        )


def test_local_override_is_not_the_deploy_time_github_token_var(monkeypatch):
    """``GITHUB_TOKEN`` is CDK's DEPLOY-time seed var; a developer with it exported for some
    unrelated tool must not silently redirect the running server's credential."""
    monkeypatch.delenv(github_token.LOCAL_TOKEN_ENV_VAR, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", _fake_token("unrelatedtoolvalue0000000"))
    client = _FakeClient(value=_fake_token("secretsmanagervalue000000"))
    with mock.patch("boto3.client", return_value=client):
        assert (
            github_token.resolve_github_token() == _fake_token("secretsmanagervalue000000")
        )
