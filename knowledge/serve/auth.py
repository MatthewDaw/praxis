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

# Tolerate small clock skew between this machine, Cognito, and the backend so a
# freshly-minted token isn't rejected with "not yet valid (iat)" when the local
# clock is a second or two behind AWS. Applies to iat/nbf/exp checks.
_CLOCK_SKEW_LEEWAY = 300  # seconds


def _dev_principal() -> "Principal":
    """The auth-bypass principal used when ``PRAXIS_AUTH_DISABLED=1``.

    Defaults to the synthetic ``dev-user`` (what tests rely on). For local dev
    against a real graph, set ``PRAXIS_DEV_USER_SUB`` to a real Cognito ``sub``
    to impersonate that user — membership, ``/me``, and the tenant ``user_id``
    all key off this, so the bypass then sees that user's real orgs and data.
    """
    sub = os.environ.get("PRAXIS_DEV_USER_SUB", "").strip() or "dev-user"
    email = os.environ.get("PRAXIS_DEV_USER_EMAIL", "").strip() or "dev@local"
    return Principal(sub=sub, email=email)


@dataclass
class Principal:
    sub: str
    email: str | None
    # When the request authenticated via an API key, the org that key is scoped
    # to (else None for Cognito/dev principals). The org dependency enforces that
    # the request's X-Praxis-Org matches this.
    api_key_org: str | None = None


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


def _cognito_configured() -> bool:
    """True iff this backend has a Cognito pool + client wired (can verify bearers).

    A backend with these unset is API-key-only: a bearer can never validate here,
    so we say so by name instead of returning a generic 401 (see :func:`_bearer_error`).
    """
    cfg = cognito_config()
    return bool(cfg.user_pool_id and cfg.client_id)


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
            leeway=_CLOCK_SKEW_LEEWAY,
        )
    else:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=cfg.issuer,
            leeway=_CLOCK_SKEW_LEEWAY,
        )
        if claims.get("client_id") != cfg.client_id:
            raise jwt.InvalidTokenError("client_id mismatch")
    return claims


def _bearer_error(token: str, exc: Exception) -> str:
    """A precise 401 detail for a bearer that failed verification.

    Names the specific failure mode instead of a bare "invalid token": a token
    from a DIFFERENT Cognito pool (the classic "right key, wrong backend" case)
    is called out as a pool mismatch, so the caller learns to point at the
    backend that actually hosts their org rather than guessing.
    """
    cfg = cognito_config()
    try:
        iss = jwt.decode(token, options={"verify_signature": False}).get("iss")
    except Exception:  # noqa: BLE001 — undecodable token: fall through to the generic reason
        iss = None
    if iss and iss != cfg.issuer:
        return (
            f"bearer pool mismatch: token was issued by {iss!r} but this backend only trusts "
            f"{cfg.issuer!r} (pool {cfg.user_pool_id!r}). Use a token minted against this pool, or "
            f"authenticate with an X-Praxis-Key for the org this backend hosts."
        )
    return f"invalid token: {exc}"


def _principal_from_jwt(authorization: str | None) -> Principal:
    """Resolve a Principal from a Cognito Bearer JWT (or the dev seam)."""
    if os.environ.get("PRAXIS_AUTH_DISABLED") == "1":
        return _dev_principal()

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")

    # A backend with no Cognito pool wired can NEVER validate a bearer. Say so by
    # name (the expected auth mode) rather than letting the mint below fail with a
    # generic 401 that reads as "your token is bad" when it's really "wrong door".
    if not _cognito_configured():
        raise HTTPException(
            status_code=401,
            detail=(
                "this backend is API-key-only: Cognito bearer auth is not configured here "
                "(COGNITO_USER_POOL_ID / COGNITO_CLIENT_ID are unset). Send an "
                "'X-Praxis-Key: pxk_...' header instead."
            ),
        )

    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = verify_token(token)
    except Exception as exc:  # noqa: BLE001 - any decode/JWKS failure is a 401
        raise HTTPException(status_code=401, detail=_bearer_error(token, exc)) from exc

    return Principal(sub=claims["sub"], email=claims.get("email"))


def current_user(authorization: str = Header(None)) -> Principal:
    """FastAPI dependency: resolve the caller's ``Principal`` from a Bearer JWT.

    Kept for callers (and tests) that don't need API-key auth. The server wires
    :func:`make_current_user` instead so it can also accept ``X-Praxis-Key``.
    """
    return _principal_from_jwt(authorization)


def make_current_user(conn):
    """Build a ``current_user`` dependency that also accepts an API key.

    A request authenticates EITHER via the existing Cognito Bearer JWT OR via the
    ``X-Praxis-Key: pxk_...`` header. An API key resolves to a Principal whose
    ``sub`` is the key's ``user_id`` (if set) else ``apikey:<id>``, and pins
    ``api_key_org`` to the key's org so the org dependency can enforce a match.
    The ``PRAXIS_AUTH_DISABLED=1`` dev seam still short-circuits to a dev
    principal (checked first, so local runs need no key or token).
    """
    from knowledge.serve import apikeys

    def current_user(
        authorization: str = Header(None),
        x_praxis_key: str | None = Header(default=None),
    ) -> Principal:
        if os.environ.get("PRAXIS_AUTH_DISABLED") == "1":
            return _dev_principal()
        if x_praxis_key:
            record = apikeys.resolve_key(conn, x_praxis_key.strip())
            if record is None:
                raise HTTPException(status_code=401, detail="invalid API key")
            sub = record.user_id or f"apikey:{record.id}"
            return Principal(sub=sub, email=None, api_key_org=record.org_id)
        return _principal_from_jwt(authorization)

    return current_user
