"""Cognito JWT verification + the FastAPI ``current_user`` dependency.

Data routes hard-require a valid Cognito JWT (see the auth plan): the token's
``sub`` becomes the tenant ``user_id``. JWKS is fetched/cached by a module-level
``PyJWKClient``. An offline test seam (PRAXIS_AUTH_DISABLED=1) returns a fixed
dev principal so existing tests run without minting real tokens.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Header, HTTPException

DEFAULT_REGION = "us-east-1"


@dataclass
class Principal:
    sub: str
    email: str | None


@dataclass
class CognitoConfig:
    user_pool_id: str
    region: str
    client_id: str

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    @property
    def jwks_url(self) -> str:
        return self.issuer + "/.well-known/jwks.json"


@lru_cache(maxsize=1)
def cognito_config() -> CognitoConfig:
    """Read Cognito settings from the environment (cached)."""
    user_pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
    region = os.environ.get("COGNITO_REGION", DEFAULT_REGION)
    client_id = os.environ.get("COGNITO_CLIENT_ID", "")
    return CognitoConfig(user_pool_id=user_pool_id, region=region, client_id=client_id)


@lru_cache(maxsize=1)
def _jwks_client() -> jwt.PyJWKClient:
    """Module-level cached JWKS client (handles key fetch/rotation/caching)."""
    return jwt.PyJWKClient(cognito_config().jwks_url)


def verify_token(token: str) -> dict:
    """Verify a Cognito JWT and return its claims; raise on failure.

    Cognito ID tokens carry ``aud`` == client_id; access tokens carry a
    ``client_id`` claim instead. Decode with the right audience per
    ``token_use`` so both token kinds verify.
    """
    cfg = cognito_config()
    signing_key = _jwks_client().get_signing_key_from_jwt(token)
    unverified = jwt.decode(token, options={"verify_signature": False})
    token_use = unverified.get("token_use")

    if token_use == "id":
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=cfg.issuer,
            audience=cfg.client_id,
        )
    else:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=cfg.issuer,
        )
        if claims.get("client_id") != cfg.client_id:
            raise jwt.InvalidTokenError("client_id mismatch")
    return claims


def current_user(authorization: str = Header(None)) -> Principal:
    """FastAPI dependency: resolve the caller's ``Principal`` from a Bearer JWT."""
    if os.environ.get("PRAXIS_AUTH_DISABLED") == "1":
        return Principal(sub="dev-user", email="dev@local")

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")

    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = verify_token(token)
    except Exception as exc:  # noqa: BLE001 - any decode/JWKS failure is a 401
        raise HTTPException(status_code=401, detail="invalid token") from exc

    return Principal(sub=claims["sub"], email=claims.get("email"))
