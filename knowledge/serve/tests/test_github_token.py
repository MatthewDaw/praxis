"""Tests for the in-process GitHub token cache (R1: Secrets Manager storage).

The backing store is AWS Secrets Manager; boto3 is mocked so no real AWS call is
made. Covers: fetch-once-per-process caching, invalidate-on-auth-failure refresh,
graceful failure to ``None``, and the "never logged" guarantee (R1 acceptance).
"""

from __future__ import annotations

import logging
from unittest import mock

import pytest

from knowledge.serve import github_token


@pytest.fixture(autouse=True)
def _reset_cache():
    github_token.invalidate_github_token_cache()
    yield
    github_token.invalidate_github_token_cache()


def _mock_client(value):
    client = mock.Mock()
    client.get_secret_value.return_value = {"SecretString": value}
    return client


# Deliberately NOT shaped like a real GitHub token (no "ghp_"/"github_pat_" prefix
# followed by 10+ token chars) so the repo-wide token-leak scan (R1's own gate)
# never flags these test fixtures as a leaked credential.
FAKE_TOKEN = "test-fixture-token-one"
FAKE_TOKEN_2 = "test-fixture-token-two"
FAKE_TOKEN_SECRET = "test-fixture-supersecret-value"


def test_fetches_once_per_process_and_caches():
    client = _mock_client(FAKE_TOKEN)
    with mock.patch("boto3.client", return_value=client) as boto_client:
        token1 = github_token.get_github_token()
        token2 = github_token.get_github_token()
    assert token1 == FAKE_TOKEN
    assert token2 == FAKE_TOKEN
    boto_client.assert_called_once()
    assert client.get_secret_value.call_count == 1


def test_invalidate_forces_a_refetch_on_next_call():
    client = _mock_client(FAKE_TOKEN)
    with mock.patch("boto3.client", return_value=client):
        first = github_token.get_github_token()
        github_token.invalidate_github_token_cache()  # e.g. after an upstream 401
        client.get_secret_value.return_value = {"SecretString": FAKE_TOKEN_2}
        second = github_token.get_github_token()
    assert first == FAKE_TOKEN
    assert second == FAKE_TOKEN_2
    assert client.get_secret_value.call_count == 2


def test_force_refresh_flag_also_refetches():
    client = _mock_client(FAKE_TOKEN)
    with mock.patch("boto3.client", return_value=client):
        github_token.get_github_token()
        client.get_secret_value.return_value = {"SecretString": FAKE_TOKEN_2}
        refreshed = github_token.get_github_token(force_refresh=True)
    assert refreshed == FAKE_TOKEN_2
    assert client.get_secret_value.call_count == 2


def test_missing_secret_resolves_to_none_not_an_exception():
    client = mock.Mock()
    client.get_secret_value.side_effect = Exception("no creds")
    with mock.patch("boto3.client", return_value=client):
        assert github_token.get_github_token() is None


def test_token_value_is_never_logged(caplog):
    client = _mock_client(FAKE_TOKEN_SECRET)
    caplog.set_level(logging.DEBUG)
    with mock.patch("boto3.client", return_value=client):
        github_token.get_github_token()
    for record in caplog.records:
        assert FAKE_TOKEN_SECRET not in record.getMessage()
