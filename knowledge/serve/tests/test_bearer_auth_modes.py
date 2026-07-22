"""Bearer 401s must NAME the auth mode, not just say "invalid token" (blocker #2).

Offline (no Cognito network): we drive ``_principal_from_jwt`` directly and assert
the 401 detail is actionable:
  * a backend with NO Cognito pool wired says it is API-key-only (wrong door),
  * a token from a DIFFERENT pool says "bearer pool mismatch" (right key, wrong backend),
  * a same-pool-but-unverifiable token falls back to a plain "invalid token".
"""

from __future__ import annotations

import jwt
import pytest
from fastapi import HTTPException

from knowledge.serve import auth

CONFIGURED_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_bestiePool"


@pytest.fixture(autouse=True)
def _enable_auth(monkeypatch):
    monkeypatch.delenv("PRAXIS_AUTH_DISABLED", raising=False)
    yield
    auth.cognito_config.cache_clear()
    auth._jwks_client.cache_clear()


def _configure_pool(monkeypatch, pool="us-east-1_bestiePool", client="bestieClient"):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", pool)
    monkeypatch.setenv("COGNITO_CLIENT_ID", client)
    monkeypatch.setenv("COGNITO_REGION", "us-east-1")
    auth.cognito_config.cache_clear()
    auth._jwks_client.cache_clear()


def test_api_key_only_backend_names_the_mode(monkeypatch):
    # No Cognito pool wired -> a bearer can NEVER validate here; say so by name.
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "")
    auth.cognito_config.cache_clear()
    with pytest.raises(HTTPException) as exc:
        auth._principal_from_jwt("Bearer whatever.token.here")
    assert exc.value.status_code == 401
    detail = exc.value.detail
    assert "API-key-only" in detail
    assert "X-Praxis-Key" in detail
    assert "COGNITO_USER_POOL_ID" in detail


def test_wrong_pool_bearer_reports_pool_mismatch(monkeypatch):
    _configure_pool(monkeypatch)
    # A syntactically valid JWT issued by a DIFFERENT pool (foreign issuer).
    foreign = jwt.encode(
        {"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_sotosPool", "sub": "u1"},
        "k",
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as exc:
        auth._principal_from_jwt(f"Bearer {foreign}")
    assert exc.value.status_code == 401
    assert "bearer pool mismatch" in exc.value.detail
    assert "us-east-1_sotosPool" in exc.value.detail  # names the token's pool
    assert CONFIGURED_ISSUER in exc.value.detail       # names the trusted pool


def test_same_pool_unverifiable_token_is_plain_invalid(monkeypatch):
    _configure_pool(monkeypatch)
    # Same (trusted) issuer, but no valid RS256 signature -> generic invalid token.
    same = jwt.encode({"iss": CONFIGURED_ISSUER, "sub": "u1"}, "k", algorithm="HS256")
    with pytest.raises(HTTPException) as exc:
        auth._principal_from_jwt(f"Bearer {same}")
    assert exc.value.status_code == 401
    assert exc.value.detail.startswith("invalid token")
    assert "pool mismatch" not in exc.value.detail


def test_bearer_error_helper_directly(monkeypatch):
    _configure_pool(monkeypatch)
    foreign = jwt.encode(
        {"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_other", "sub": "u"},
        "k",
        algorithm="HS256",
    )
    msg = auth._bearer_error(foreign, ValueError("boom"))
    assert "bearer pool mismatch" in msg
    # An undecodable token can't reveal an issuer -> generic reason, no crash.
    assert auth._bearer_error("garbage", ValueError("boom")) == "invalid token: boom"
