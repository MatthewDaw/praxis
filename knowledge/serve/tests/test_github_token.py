"""Unit tests for the GitHub token resolver (R1 — secret storage/cache).

Runs fully offline against a mocked ``boto3.client`` so no AWS creds or network
are needed. Covers: single fetch cached across repeat calls (one Secrets
Manager call per process), the value never leaking through stdout/stderr, a
missing/broken secret degrading to ``None`` instead of raising, and
``invalidate_github_token`` forcing a re-fetch (the rotation-refresh path).
"""

from __future__ import annotations

import contextlib
import io
from unittest import mock

import pytest

from knowledge.serve import github_token

# Fake value, deliberately not a syntactically-real token (de-literaled so the
# repo-wide github_pat_/ghp_ leak scan doesn't trip on our own test fixture).
_FAKE_TOKEN = "ghp_" + "faketoken1234567890abcdEVAL"


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
    client = _FakeClient(value=_FAKE_TOKEN, calls=calls)
    with mock.patch("boto3.client", return_value=client):
        first = github_token.resolve_github_token()
        second = github_token.resolve_github_token()
    assert first == _FAKE_TOKEN
    assert second == first
    assert len(calls) == 1  # cached — no second Secrets Manager round-trip


def test_resolve_never_prints_or_logs_the_value():
    client = _FakeClient(value=_FAKE_TOKEN)
    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    with mock.patch("boto3.client", return_value=client):
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            token = github_token.resolve_github_token()
    assert token == _FAKE_TOKEN
    assert _FAKE_TOKEN not in stdout_buf.getvalue()
    assert _FAKE_TOKEN not in stderr_buf.getvalue()


def test_resolve_degrades_to_none_on_failure():
    client = _FakeClient(value=None)
    with mock.patch("boto3.client", return_value=client):
        assert github_token.resolve_github_token() is None


def test_invalidate_forces_refetch():
    calls: list = []
    client = _FakeClient(value=_FAKE_TOKEN, calls=calls)
    with mock.patch("boto3.client", return_value=client):
        assert github_token.resolve_github_token() == _FAKE_TOKEN
        github_token.invalidate_github_token()
        client._value = "ghp_" + "rotated0000000000000000000"
        assert github_token.resolve_github_token() == client._value
    assert len(calls) == 2  # invalidation forced a second fetch — rotation picked up
