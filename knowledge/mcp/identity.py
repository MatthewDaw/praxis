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
_DEFAULT_CACHE_PATH = Path.home() / ".praxis" / "mcp.json"

_LOGIN_HINT = "not logged in — ask Claude to log in (the praxis_login tool)"


def cache_path() -> Path:
    """Where the cached identity (login + active org) lives.

    Defaults to ``~/.praxis/mcp.json`` but is overridable per process via the
    ``PRAXIS_MCP_CACHE`` env var. This is what lets two MCP servers on one machine
    each pin a DIFFERENT active org (and even a different login): point each at its
    own cache file and the ``X-Praxis-Org`` header they send diverges, so each agent
    drives its own ``(org_id, user_id)`` tenant without clobbering the other's org.
    """
    override = os.environ.get("PRAXIS_MCP_CACHE", "").strip()
    return Path(override).expanduser() if override else _DEFAULT_CACHE_PATH


@dataclass
class Tenant:
    refresh_token: str
    sub: str
    email: str | None
    org_id: str
    api_base: str
    # Purely LOCAL client-side default fed to the ``space`` param of snapshot/mount
    # tools (via ``praxis_select_space``). Never sent as a header; never selects a
    # working graph — working-memory ops always resolve to the authenticated ``sub``.
    space_id: str = ""


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
    """Persist the tenant to the active cache path (see :func:`cache_path`) mode 600."""
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "refresh_token": tenant.refresh_token,
                "sub": tenant.sub,
                "email": tenant.email,
                "org_id": tenant.org_id,
                "api_base": tenant.api_base,
                "space_id": tenant.space_id,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def load_identity() -> Tenant:
    """Load the cached tenant; raise a clear hint if the user hasn't logged in."""
    path = cache_path()
    if not path.exists():
        raise RuntimeError(_LOGIN_HINT)
    data = json.loads(path.read_text(encoding="utf-8"))
    return Tenant(
        refresh_token=data["refresh_token"],
        sub=data["sub"],
        email=data.get("email"),
        org_id=data["org_id"],
        api_base=data.get("api_base", DEFAULT_API_BASE),
        space_id=data.get("space_id", ""),
    )


def is_logged_in() -> bool:
    """True once a login has been cached."""
    return cache_path().exists()


def pinned_org() -> str:
    """The explicit ``PRAXIS_ORG`` env pin, stripped (``""`` if unset). The highest-precedence org."""
    return os.environ.get("PRAXIS_ORG", "").strip()


def resolve_org(pinned: str, cached: str, default: str = "") -> str:
    """THE org-precedence rule, in one place: explicit ``PRAXIS_ORG`` pin > cached selection > default.

    This is the single authority both the MCP layer (:func:`active_org`) and the Stop-hook client
    (``agent_factory/hooks/_praxis.py:_resolve_org``, a stdlib-only mirror) resolve the active org
    with, so ``praxis_whoami`` / ``praxis_select_org`` and what ``add_insight`` / ``facts_by`` / the
    factory hooks actually send as ``X-Praxis-Org`` can never diverge. Pure — no I/O, no env reads.
    """
    return (pinned or "").strip() or (cached or "").strip() or default


def active_org() -> str:
    """The active org id actually sent as ``X-Praxis-Org``; ``""`` if none chosen.

    Resolved via :func:`resolve_org`: a ``PRAXIS_ORG`` env var PINS the org and WINS over the cached
    value. This is the per-project org pin (the sibling of ``PRAXIS_MCP_CACHE``): the cached
    ``org_id`` in ``mcp.json`` is mutable — a ``praxis_login`` / ``set_org`` from ANY server sharing
    the cache (or an auto-select on reconnect) can silently flip it to another org. Pinning
    ``PRAXIS_ORG`` in a project's MCP env makes that project's tenant deterministic and immune to a
    sibling agent's org switch (the reported reconnect-flips-to-team-app bug).

    Because THIS is what the header carries, ``praxis_whoami`` reports it (not the raw cached
    ``org_id``) and ``praxis_select_org`` refuses a selection a live pin would contradict — so the
    org whoami reports is always the org writes actually hit.
    """
    pinned = pinned_org()
    # Only touch the cache when there is no pin, so a pinned project need not be logged in to resolve.
    cached = "" if pinned else load_identity().org_id
    return resolve_org(pinned, cached)


def factory_org() -> str:
    """The org THIS factory session operates in — **derived from the project config, never hardcoded**.

    A factory project pins its own org via ``PRAXIS_ORG`` (in ``<project>/.claude/settings.local.json``)
    and/or its per-project MCP cache (``PRAXIS_MCP_CACHE``); the tickets/checks live in THAT org. This is
    the single "resolve the factory org for this project" entry point the af-* skills mean by "the factory
    org": it is exactly :func:`active_org` (``PRAXIS_ORG`` pin > cached selection), named so the memory
    policy can say "operate in ``factory_org()``" instead of hardcoding ``agent-factory``. The Stop-hook
    client resolves the SAME value via its stdlib mirror (``hooks/_praxis.py:_resolve_org`` in
    ``_auth_headers``), so the MCP-tool org and the hook-client org always agree — the one hard rule.
    """
    return active_org()


def set_org(org_id: str) -> Tenant:
    """Set the active org on the cached identity and persist it."""
    tenant = load_identity()
    tenant.org_id = org_id
    save_identity(tenant)
    return tenant


def active_space() -> str:
    """The client-side default ``space`` for snapshot/mount ops; ``""`` if none chosen.

    This is a purely LOCAL default for the ``space`` PARAMETER of the snapshot/mount
    tools (set via ``praxis_select_space``). It is NEVER emitted as a header and NEVER
    selects a working graph — working-memory ops always resolve to the authenticated
    ``sub``.

    Unlike :func:`active_org`, this never raises when there is no cached login: the
    default is optional (absent == no default), so a missing/unreadable cache resolves
    to ``""``.
    """
    try:
        return load_identity().space_id
    except Exception:  # noqa: BLE001 - not-logged-in / corrupt cache => no default
        return ""


def set_space(space_id: str) -> Tenant:
    """Set the client-side default ``space`` on the cached identity and persist it."""
    tenant = load_identity()
    tenant.space_id = space_id
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
    """The backend base URL: cached value, else the ``PRAXIS_API_BASE_URL`` env.

    In the MCP client's auth-disabled seam (``PRAXIS_MCP_AUTH_DISABLED=1``) there
    may be no cached login, so fall back to the env/default base instead of
    requiring one.
    """
    if os.environ.get("PRAXIS_MCP_AUTH_DISABLED") == "1":
        return _api_base()
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
    # Renew straight from the refresh token. ``check_token()`` can't be used
    # here: it inspects an existing access token to decide whether to renew, and
    # this object only carries a refresh token, so it raises "Access Token
    # Required to Check Token". ``renew_access_token`` mints fresh id/access
    # tokens from the refresh token unconditionally.
    cog.renew_access_token()
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
