"""Unit tests for Cognito JWT verification + the current_user dependency.

These run offline: verify_token rejects garbage without any network (the decode
fails before/at signature verification), and current_user enforces 401 when auth
is enabled and no Bearer header is supplied.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from knowledge.serve import auth


def test_verify_token_rejects_garbage(monkeypatch):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "client")
    auth.cognito_config.cache_clear()
    auth._jwks_client.cache_clear()
    with pytest.raises(Exception):
        auth.verify_token("not-a-jwt")


def test_current_user_disabled_returns_dev_principal(monkeypatch):
    monkeypatch.setenv("PRAXIS_AUTH_DISABLED", "1")
    p = auth.current_user(authorization=None)
    assert p.sub == "dev-user"
    assert p.email == "dev@local"


def test_current_user_401_without_header(monkeypatch):
    monkeypatch.delenv("PRAXIS_AUTH_DISABLED", raising=False)
    with pytest.raises(HTTPException) as exc:
        auth.current_user(authorization=None)
    assert exc.value.status_code == 401


def test_current_user_401_on_bad_scheme(monkeypatch):
    monkeypatch.delenv("PRAXIS_AUTH_DISABLED", raising=False)
    with pytest.raises(HTTPException) as exc:
        auth.current_user(authorization="Basic abc")
    assert exc.value.status_code == 401
