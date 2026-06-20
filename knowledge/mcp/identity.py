"""Cognito login + cached-token identity for the MCP client.

Login is driven by the MCP tools (``praxis_login`` etc.), not a CLI step:
:func:`authenticate` authenticates against the deployed Cognito pool (via
``pycognito``), verifies the ID token with the backend's
:func:`knowledge.serve.auth.verify_token`, and caches ``{refresh_token, sub,
email, org_id, api_base}`` to ``~/.praxis/mcp.json`` (mode 600). The served
process only mints fresh ID tokens from the cached refresh token + reads the
cache. (The interactive :func:`login` is kept for occasional CLI use.)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from pycognito import Cognito

from knowledge.serve.auth import verify_token

DEFAULT_API_BASE = "http://localhost:8000"
CACHE_PATH = Path.home() / ".praxis" / "mcp.json"

_LOGIN_HINT = "not logged in — ask Claude to log in (the praxis_login tool)"


@dataclass
class Tenant:
    refresh_token: str
    sub: str
    email: str | None
    org_id: str
    api_base: str


def _cognito_env() -> tuple[str, str, str]:
    """Cognito pool/client/region from the environment (matches serve/auth)."""
    user_pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
    client_id = os.environ.get("COGNITO_CLIENT_ID", "")
    region = os.environ.get("COGNITO_REGION", "us-east-1")
    return user_pool_id, client_id, region


def _api_base() -> str:
    return os.environ.get("PRAXIS_API_BASE_URL", DEFAULT_API_BASE).rstrip("/")


def _cognito(username: str | None = None) -> Cognito:
    user_pool_id, client_id, region = _cognito_env()
    return Cognito(
        user_pool_id,
        client_id,
        user_pool_region=region,
        username=username,
    )


def save_identity(tenant: Tenant) -> None:
    """Persist the tenant to ``~/.praxis/mcp.json`` with owner-only perms."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(
            {
                "refresh_token": tenant.refresh_token,
                "sub": tenant.sub,
                "email": tenant.email,
                "org_id": tenant.org_id,
                "api_base": tenant.api_base,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(CACHE_PATH, 0o600)


def load_identity() -> Tenant:
    """Load the cached tenant; raise a clear hint if the user hasn't logged in."""
    if not CACHE_PATH.exists():
        raise RuntimeError(_LOGIN_HINT)
    data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return Tenant(
        refresh_token=data["refresh_token"],
        sub=data["sub"],
        email=data.get("email"),
        org_id=data["org_id"],
        api_base=data.get("api_base", DEFAULT_API_BASE),
    )


def is_logged_in() -> bool:
    """True once a login has been cached."""
    return CACHE_PATH.exists()


def active_org() -> str:
    """The cached active org id (``X-Praxis-Org`` value); ``""`` if none chosen."""
    return load_identity().org_id


def set_org(org_id: str) -> Tenant:
    """Set the active org on the cached identity and persist it."""
    tenant = load_identity()
    tenant.org_id = org_id
    save_identity(tenant)
    return tenant


def list_my_orgs() -> list[dict]:
    """The orgs the logged-in user belongs to (backend ``GET /me``)."""
    return _list_orgs(api_base(), token())


def authenticate(email: str, password: str) -> tuple[Tenant, list[dict]]:
    """Cognito login + cache, without prompting; return (tenant, the user's orgs).

    Non-interactive counterpart to :func:`login` for the MCP tool path. Auto-
    selects the org when the user has exactly one; otherwise leaves ``org_id``
    empty for the caller to set via :func:`set_org`.
    """
    cog = _cognito(username=email)
    cog.authenticate(password=password)
    claims = verify_token(cog.id_token)

    api = _api_base()
    orgs = _list_orgs(api, cog.id_token)
    org_id = ""
    if len(orgs) == 1:
        org_id = str(orgs[0].get("orgId") or orgs[0].get("org_id") or "")

    tenant = Tenant(
        refresh_token=cog.refresh_token,
        sub=claims["sub"],
        email=claims.get("email", email),
        org_id=org_id,
        api_base=api,
    )
    save_identity(tenant)
    return tenant, orgs


def api_base() -> str:
    """The backend base URL: cached value, else the ``PRAXIS_API_BASE_URL`` env."""
    return load_identity().api_base


def token() -> str:
    """Mint a fresh ID token from the cached refresh token."""
    tenant = load_identity()
    user_pool_id, client_id, region = _cognito_env()
    cog = Cognito(
        user_pool_id,
        client_id,
        user_pool_region=region,
        refresh_token=tenant.refresh_token,
    )
    cog.check_token()  # renews via the refresh token when expired
    return cog.id_token


def _list_orgs(api: str, id_token: str) -> list[dict]:
    resp = httpx.get(
        f"{api}/me",
        headers={"Authorization": f"Bearer {id_token}"},
    )
    resp.raise_for_status()
    return resp.json().get("orgs", [])


def _pick_org(orgs: list[dict]) -> str:
    """Prompt the user to pick one of their orgs (or accept a single one)."""
    if not orgs:
        return input("No orgs found — enter an org id to use: ").strip()
    if len(orgs) == 1:
        return str(orgs[0].get("orgId") or orgs[0].get("org_id"))
    print("Your orgs:")
    for i, org in enumerate(orgs):
        oid = org.get("orgId") or org.get("org_id")
        print(f"  [{i}] {oid} ({org.get('role', 'member')})")
    choice = int(input("Pick an org by number: ").strip())
    org = orgs[choice]
    return str(org.get("orgId") or org.get("org_id"))


def login(email: str, password: str) -> Tenant:
    """Authenticate with Cognito, pick an org, and cache the identity."""
    cog = _cognito(username=email)
    cog.authenticate(password=password)
    claims = verify_token(cog.id_token)

    api = _api_base()
    orgs = _list_orgs(api, cog.id_token)
    org_id = _pick_org(orgs)

    tenant = Tenant(
        refresh_token=cog.refresh_token,
        sub=claims["sub"],
        email=claims.get("email", email),
        org_id=org_id,
        api_base=api,
    )
    save_identity(tenant)
    return tenant
