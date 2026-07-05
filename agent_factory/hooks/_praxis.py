#!/usr/bin/env python3
"""
Dependency-light Praxis HTTP client for the factory's Stop-hook subprocesses.

Praxis is the SINGLE SOURCE OF DYNAMIC TRUTH for the factory (tickets, checks, and the
outcomes/state that say what is built/passed). A Stop-hook gate must read that truth LIVE.
Because hooks run as bare Python subprocesses with no virtualenv, this module uses ONLY the
stdlib (``urllib.request`` / ``json``) — no ``httpx``, no ``pycognito``, no ``praxis`` import.

FAIL-CLOSED CONTRACT
--------------------
Praxis is a HARD dependency. If it is unreachable, or auth cannot be established, or the server
returns an error, every method raises :class:`PraxisUnreachable`. Callers (gates) MUST treat that
as a BLOCK — they may never fail open. A gate that cannot prove the truth must not let work pass.

AUTH
----
Headers sent on every request:
  * ``x-praxis-key``  — from ``PRAXIS_API_KEY`` if set (preferred, simplest).
  * ``Authorization: Bearer <id_token>`` — else a fresh Cognito ID token minted from the cached
    refresh token in ``~/.praxis/mcp.json`` (replicating ``knowledge/mcp/identity.py:token()``
    WITHOUT importing the praxis package — a raw Cognito ``InitiateAuth`` REFRESH_TOKEN_AUTH call).
    If neither an API key nor a usable Cognito mint is available, we FAIL CLOSED.
  * ``x-praxis-org``  — from ``PRAXIS_ORG`` (default ``agent-factory``).
  * ``x-praxis-space``— from ``PRAXIS_SPACE`` only if set (absent == default graph).

The base URL is ``PRAXIS_API_BASE_URL`` (default ``http://localhost:8000``).
The ``PRAXIS_AUTH_DISABLED=1`` dev seam is honored: when set we skip auth entirely (the server's
matching seam accepts unauthenticated requests).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_API_BASE = "http://localhost:8000"
DEFAULT_ORG = "agent-factory"
_CACHE_PATH = Path.home() / ".praxis" / "mcp.json"
_HTTP_TIMEOUT_S = 10


def _load_dotenv() -> None:
    """Load repo-root ``.env`` into ``os.environ`` (without overriding already-set vars).

    A Stop-hook runs as a bare subprocess that does NOT inherit a shell-sourced ``.env``, so the
    factory's Praxis credentials (``PRAXIS_API_KEY``/``PRAXIS_ORG``/...) live in ``<repo>/.env`` and
    must be loaded explicitly. Stdlib-only, tolerant ``KEY=VALUE`` parsing (skips blanks/comments,
    strips optional surrounding quotes). Real environment values WIN over the file, so an operator
    can always override. Searched newest-wins: repo root (``hooks/..``), cwd, the hooks dir itself.
    """
    candidates = [
        Path(__file__).resolve().parent.parent / ".env",  # <repo>/.env (hooks/ is at repo root)
        Path.cwd() / ".env",
        Path(__file__).resolve().parent / ".env",
    ]
    for env_path in candidates:
        try:
            if not env_path.is_file():
                continue
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:  # never override a real env var
                    os.environ[key] = val
        except Exception:  # noqa: BLE001 — a malformed .env must not crash the gate
            continue


_load_dotenv()


class PraxisUnreachable(RuntimeError):
    """Praxis could not be reached / authenticated / queried. Callers MUST fail closed (BLOCK)."""


# --------------------------------------------------------------------------- auth

def _api_base() -> str:
    return os.environ.get("PRAXIS_API_BASE_URL", DEFAULT_API_BASE).rstrip("/")


def _auth_disabled() -> bool:
    return os.environ.get("PRAXIS_AUTH_DISABLED") == "1"


def _mint_cognito_token() -> str:
    """Mint a fresh Cognito ID token from the cached refresh token, stdlib-only.

    Minimal replication of ``knowledge/mcp/identity.py:token()`` (which uses pycognito's
    ``renew_access_token``): a raw ``InitiateAuth`` REFRESH_TOKEN_AUTH call against the Cognito
    IDP REST endpoint. Reads the refresh token from ``~/.praxis/mcp.json`` and the pool/client/
    region from ``COGNITO_*`` env. FAILS CLOSED (raises) if anything is missing or the call fails.
    """
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        refresh_token = data["refresh_token"]
    except Exception as exc:  # noqa: BLE001
        raise PraxisUnreachable(
            f"no Praxis auth: PRAXIS_API_KEY unset and ~/.praxis/mcp.json unreadable ({exc})"
        ) from exc

    client_id = os.environ.get("COGNITO_CLIENT_ID", "")
    region = os.environ.get("COGNITO_REGION", "us-east-1")
    if not client_id:
        raise PraxisUnreachable(
            "no Praxis auth: PRAXIS_API_KEY unset and COGNITO_CLIENT_ID missing — cannot mint a token"
        )

    url = f"https://cognito-idp.{region}.amazonaws.com/"
    body = json.dumps({
        "AuthFlow": "REFRESH_TOKEN_AUTH",
        "ClientId": client_id,
        "AuthParameters": {"REFRESH_TOKEN": refresh_token},
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise PraxisUnreachable(f"Cognito token mint failed: {exc}") from exc

    token = (payload.get("AuthenticationResult") or {}).get("IdToken")
    if not token:
        raise PraxisUnreachable("Cognito token mint returned no IdToken")
    return token


# Cache the minted token for the lifetime of the (short-lived) hook process.
_TOKEN_CACHE: dict[str, Any] = {"token": None, "exp": 0.0}


def _auth_headers() -> dict[str, str]:
    """Build the auth + tenancy headers, failing closed if no credential is available."""
    headers: dict[str, str] = {}

    org = os.environ.get("PRAXIS_ORG", DEFAULT_ORG).strip() or DEFAULT_ORG
    headers["x-praxis-org"] = org
    space = os.environ.get("PRAXIS_SPACE", "").strip()
    if space:
        headers["x-praxis-space"] = space

    if _auth_disabled():
        return headers

    api_key = os.environ.get("PRAXIS_API_KEY", "").strip()
    if api_key:
        headers["x-praxis-key"] = api_key
        return headers

    # No API key -> mint (and briefly cache) a Cognito bearer.
    now = time.time()
    if not _TOKEN_CACHE["token"] or now >= _TOKEN_CACHE["exp"]:
        _TOKEN_CACHE["token"] = _mint_cognito_token()
        _TOKEN_CACHE["exp"] = now + 600  # re-mint every ~10 min within a long-lived process
    headers["Authorization"] = f"Bearer {_TOKEN_CACHE['token']}"
    return headers


# --------------------------------------------------------------------------- transport

def _request(method: str, path: str, *, params: dict | None = None,
             body: dict | None = None, not_found_ok: bool = False,
             space: str | None = None) -> Any:
    """Issue one HTTP request and return parsed JSON, or raise PraxisUnreachable (fail-closed).

    ``space`` overrides the ``x-praxis-space`` tenancy header for THIS request only (default:
    the ``PRAXIS_SPACE`` env value). This is the checks-space seam: check RESOLUTION reads from a
    dedicated validation space while ticket STATE stays in the default/plan space — one request's
    override never leaks into the process-wide default.
    """
    base = _api_base()
    url = base + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)

    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = _auth_headers()
    if space:  # per-request tenancy override (checks-space seam) — beats the PRAXIS_SPACE default
        headers["x-praxis-space"] = space
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        # A benign 404 (route/resource not found, e.g. a surface with no checks endpoint) is NOT
        # "Praxis unreachable" — the round-trip succeeded. Callers that opt in get an empty result
        # so a supplementary lookup never fail-closes the whole operation. Everything else raises.
        if exc.code == 404 and not_found_ok:
            return {}
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise PraxisUnreachable(f"Praxis {method} {path} -> HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001  (URLError, timeout, JSON, ...)
        raise PraxisUnreachable(f"Praxis {method} {path} failed: {exc}") from exc


# --------------------------------------------------------------------------- public API

def incomplete_requirements(project: str, *, exclude_leased: bool = False) -> list[dict]:
    """Active requirements in ``prd-<project>`` not yet verified-complete (never-built |
    regressed | stale). Each item carries a ``claim`` view (build_state/claim_owner/
    claim_heartbeat_at/lease_live). ``exclude_leased=True`` omits live-leased tickets.

    CRITICAL — pass the BARE project name. The endpoint PREPENDS ``prd-`` itself (server does
    ``source = f"prd-{project}"``). So a caller that hands us an already-prefixed ``prd-team-app``
    would otherwise be searched for as ``prd-prd-team-app`` → EMPTY → a Stop gate would WRONGLY
    believe every build is complete (fail-OPEN). To make a doubly-prefixed name impossible, we strip
    a single leading ``prd-`` here before querying, so both ``"team-app"`` and ``"prd-team-app"``
    resolve to the same bare ``team-app`` the server expects.
    """
    bare = project
    while bare.startswith("prd-"):  # strip EVERY leading prd- so a double-prefix can't fail open
        bare = bare[len("prd-"):]
    out = _request("GET", "/requirements/incomplete",
                   params={"project": bare, "exclude_leased": str(exclude_leased).lower()})
    return out.get("requirements") or out.get("incomplete") or out.get("items") or []


def get_fact(cid: str) -> dict:
    """Full fact (candidate view) including ``meta``. Raises PraxisUnreachable on any error."""
    return _request("GET", f"/candidates/{cid}")


def facts_by(category: str | None = None, meta: dict | None = None,
             state: str = "active", space: str | None = None) -> list[dict]:
    """EXHAUSTIVE, server-side filtered fact enumeration (no top-k). ``meta`` is a flat object
    whose keys match by scalar equality OR array-membership. ``state`` defaults to ``active``
    (pass ``"any"`` to span all lifecycle states). ``space`` overrides the tenancy space for this
    read only (the checks-space seam — resolve reads validation checks from a dedicated space)."""
    params: dict[str, Any] = {"state": state}
    if category is not None:
        params["category"] = category
    if meta:
        params["meta"] = json.dumps(meta)
    out = _request("GET", "/facts/by", params=params, space=space)
    return out.get("facts") or []


def patch_meta(cid: str, meta_dict: dict) -> dict:
    """MERGE ``meta_dict`` into the fact's meta (top-level key merge; nested values are replaced
    wholesale). Skips re-embed (meta-only edit). This is how ticket build_state / claim /
    pinned_checks are written. Returns the updated fact."""
    return _request("PATCH", f"/candidates/{cid}", body={"meta": meta_dict})


def record_outcome(cid: str, success: bool) -> dict:
    """Record a verified build/check outcome on the fact (POST /facts/{cid}/outcome)."""
    return _request("POST", f"/facts/{cid}/outcome", body={"success": bool(success)})


def surface_checks(project: str, screen_id: str, scope: str | None = None,
                   space: str | None = None) -> list[dict]:
    """Active ``check`` facts bound (via the ``renders`` edge) to surface (project, screen_id).
    ``space`` overrides the tenancy space for this read only (the checks-space seam), so a
    surface-bound validation check is resolved from the same dedicated space as the tag lane."""
    # screen ids can contain a slash (e.g. "admin/s-login"); encode it so the path segment is valid,
    # and tolerate a 404 (a surface with no checks endpoint must not fail-close the whole resolution —
    # the tag-match lane in resolve_checks is the authoritative one).
    seg = urllib.parse.quote(screen_id, safe="")
    out = _request("GET", f"/surfaces/{seg}/checks",
                   params={"project": project, "scope": scope}, not_found_ok=True, space=space)
    return (out or {}).get("checks") or []


def context(query: str, *, top_k: int = 10, as_of: str | None = None,
            space: str | None = None) -> list[dict]:
    """Hybrid-ranked (semantic + keyword) retrieval — the SEMANTIC lane for check discovery.
    Returns the ``hits`` list (``{id,text,score,source,scope,category,...}``). ``space`` scopes the
    read to a checks-space (the seam). An empty query returns no hits (never a blind full-scan)."""
    q = (query or "").strip()
    if not q:
        return []
    out = _request("GET", "/context",
                   params={"query": q, "top_k": top_k, "as_of": as_of}, space=space)
    return out.get("hits") or []


def ping() -> bool:
    """Best-effort liveness check used by smoke tests. Raises PraxisUnreachable if unreachable."""
    _request("GET", "/facts/by", params={"state": "active", "category": "__ping__"})
    return True
