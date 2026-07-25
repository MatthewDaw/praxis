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
