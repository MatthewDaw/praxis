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

Tenancy model (org → space → snapshot + per-user working memory): working-memory reads/writes
carry NO space header — they always resolve to ``(org, authenticated user)``. A snapshot-bound op
(reading project checks, or the mutable ``prd-<project>`` tickets) emits BOTH ``x-praxis-space`` and
``x-praxis-snapshot`` — never one without the other. There is no ``PRAXIS_SPACE`` selector anymore.

The base URL is ``PRAXIS_API_BASE_URL`` (default ``http://localhost:8000``).
The ``PRAXIS_AUTH_DISABLED=1`` dev seam is honored: when set we skip auth entirely (the server's
matching seam accepts unauthenticated requests).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple

DEFAULT_API_BASE = "http://localhost:8000"
DEFAULT_ORG = "agent-factory"
_DEFAULT_CACHE_PATH = Path.home() / ".praxis" / "mcp.json"
_HTTP_TIMEOUT_S = 10


def _cache_path() -> Path:
    """The per-agent MCP identity cache — ``PRAXIS_MCP_CACHE`` if set, else ``~/.praxis/mcp.json``.

    This mirrors ``knowledge/mcp/identity.py:cache_path()`` so a Stop-hook subprocess reads the SAME
    cache the ``praxis_*`` MCP tools write. Two agents that each pin their own ``PRAXIS_MCP_CACHE``
    (a per-project override in ``<project>/.claude/settings.local.json``) therefore mint tokens and
    resolve the active org from their OWN identity — never clobbering each other, and never needing a
    shared-file edit inside the praxis repo.
    """
    override = os.environ.get("PRAXIS_MCP_CACHE", "").strip()
    return Path(override).expanduser() if override else _DEFAULT_CACHE_PATH


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


def _resolve_org(pinned: str, cached: str, default: str) -> str:
    """THE org-precedence rule: explicit ``PRAXIS_ORG`` pin > cached selection > default.

    Stdlib-only MIRROR of ``knowledge/mcp/identity.py:resolve_org`` (the hook subprocess cannot import
    the praxis package). Keeping the two byte-identical is what guarantees ``praxis_whoami`` /
    ``praxis_select_org`` (MCP) and what the factory hooks actually send as ``X-Praxis-Org`` resolve
    the SAME active org — never a silent wrong-org split. An agent_factory test asserts they agree.
    """
    return (pinned or "").strip() or (cached or "").strip() or default


def _org_from_cache() -> str:
    """The active org id cached by ``praxis_select_org`` in this agent's MCP identity cache.

    ``praxis_select_org`` (setup STEP 3) writes ``org_id`` into the cache ``_cache_path()`` points at
    — the SAME file the MCP tools use — so reading it here makes that one selection the single source
    of truth for BOTH the MCP tools and this hook. Returns ``""`` on any problem (no cache, not logged
    in, unreadable/corrupt, no org selected) so the caller falls through to ``DEFAULT_ORG`` — this is a
    best-effort resolution, never a hard failure. An explicit ``PRAXIS_ORG`` env override still wins."""
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return str(data.get("org_id") or "").strip()
    except Exception:  # noqa: BLE001 — missing/corrupt cache -> no cached org, fall back
        return ""


def _mint_cognito_token() -> str:
    """Mint a fresh Cognito ID token from the cached refresh token, stdlib-only.

    Minimal replication of ``knowledge/mcp/identity.py:token()`` (which uses pycognito's
    ``renew_access_token``): a raw ``InitiateAuth`` REFRESH_TOKEN_AUTH call against the Cognito
    IDP REST endpoint. Reads the refresh token from ``~/.praxis/mcp.json`` and the pool/client/
    region from ``COGNITO_*`` env. FAILS CLOSED (raises) if anything is missing or the call fails.
    """
    cache = _cache_path()
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        refresh_token = data["refresh_token"]
    except Exception as exc:  # noqa: BLE001
        raise PraxisUnreachable(
            f"no Praxis auth: PRAXIS_API_KEY unset and {cache} unreadable ({exc})"
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

    # Org precedence (highest first):
    #   1. PRAXIS_ORG env — an EXPLICIT pin. Set it as a per-project override in
    #      <project>/.claude/settings.local.json ("env": {"PRAXIS_ORG": "<org>"}); a real env var
    #      wins over the shared agent_factory/.env default, so a project overrides WITHOUT any edit
    #      inside the praxis repo. (NEVER edit agent_factory/.env to point a project at its org.)
    #   2. The org selected via praxis_select_org, read from this agent's MCP cache (_org_from_cache).
    #      This makes setup STEP 3 the single source of truth for both the MCP tools and this hook, so
    #      the explicit env pin in (1) is an optional belt-and-braces, not a required workaround.
    #   3. DEFAULT_ORG — the last-resort fallback.
    # Resolved through the shared precedence rule (mirror of identity.resolve_org) so this header and
    # what praxis_whoami/select_org report can never diverge.
    org = _resolve_org(os.environ.get("PRAXIS_ORG", ""), _org_from_cache(), DEFAULT_ORG)
    headers["x-praxis-org"] = org

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
             space: str | None = None, snapshot: str | None = None) -> Any:
    """Issue one HTTP request and return parsed JSON, or raise PraxisUnreachable (fail-closed).

    ``space`` + ``snapshot`` bind THIS request to a snapshot-bound graph (project checks, or the
    mutable ``prd-<project>`` ticket snapshot). When BOTH are given we emit ``x-praxis-space`` +
    ``x-praxis-snapshot``; when BOTH are absent the request resolves to the authenticated user's
    working memory (no space header).

    FAIL-CLOSED: a PARTIAL reference (exactly one of ``space``/``snapshot``) is a misconfiguration
    and RAISES rather than silently falling back to working memory. A checks read whose snapshot
    mis-defaulted to ``None`` would otherwise hit the wrong graph, return empty, and fail a Stop
    gate OPEN — so we refuse the request instead.
    """
    if (space is None) != (snapshot is None):
        raise PraxisUnreachable(
            f"Praxis {method} {path}: partial snapshot reference "
            f"(space={space!r}, snapshot={snapshot!r}) — both or neither required; refusing to "
            "fall back to working memory"
        )

    base = _api_base()
    url = base + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)

    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = _auth_headers()
    if space is not None:  # snapshot-bound op — emit BOTH tenancy headers (partial already refused)
        headers["x-praxis-space"] = space
        headers["x-praxis-snapshot"] = snapshot  # type: ignore[assignment]  # non-None by the guard
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

def incomplete_requirements(project: str, *, exclude_leased: bool = False,
                            space: str | None = None, snapshot: str | None = None) -> list[dict]:
    """Active requirements in ``prd-<project>`` not yet verified-complete (never-built |
    regressed | stale). Each item carries a ``claim`` view (build_state/claim_owner/
    claim_heartbeat_at/lease_live). ``exclude_leased=True`` omits live-leased tickets.

    The ``prd-<project>`` ticket graph is a MUTABLE snapshot in the project space
    (``space=<project>``, ``snapshot=prd-<project>``); pass that ``(space, snapshot)`` reference to
    read tickets from the snapshot-bound serve path. Absent both, the read resolves to working
    memory (legacy default); a partial reference fails closed (see :func:`_request`).

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
                   params={"project": bare, "exclude_leased": str(exclude_leased).lower()},
                   space=space, snapshot=snapshot)
    return out.get("requirements") or out.get("incomplete") or out.get("items") or []


def get_fact(cid: str, *, space: str | None = None, snapshot: str | None = None) -> dict:
    """Full fact (candidate view) including ``meta``. Raises PraxisUnreachable on any error.
    Pass the ticket ``(space, snapshot)`` to read from a snapshot-bound graph (e.g. the mutable
    ``prd-<project>`` tickets); omit both for working memory. A partial reference fails closed."""
    return _request("GET", f"/candidates/{cid}", space=space, snapshot=snapshot)


def facts_by(category: str | None = None, meta: dict | None = None,
             state: str = "active", space: str | None = None,
             snapshot: str | None = None) -> list[dict]:
    """EXHAUSTIVE, server-side filtered fact enumeration (no top-k). ``meta`` is a flat object
    whose keys match by scalar equality OR array-membership. ``state`` defaults to ``active``
    (pass ``"any"`` to span all lifecycle states). ``(space, snapshot)`` bind this read to a
    snapshot-bound graph — the checks seam resolves validation/planning checks from the project
    space's ``building-validation`` / ``planning-validation`` snapshot; a partial reference fails
    closed (see :func:`_request`)."""
    params: dict[str, Any] = {"state": state}
    if category is not None:
        params["category"] = category
    if meta:
        params["meta"] = json.dumps(meta)
    out = _request("GET", "/facts/by", params=params, space=space, snapshot=snapshot)
    return out.get("facts") or []


def patch_meta(cid: str, meta_dict: dict, *, space: str | None = None,
               snapshot: str | None = None) -> dict:
    """MERGE ``meta_dict`` into the fact's meta (top-level key merge; nested values are replaced
    wholesale). Skips re-embed (meta-only edit). This is how ticket build_state / claim /
    pinned_checks are written. Pass the ticket ``(space, snapshot)`` to write into the mutable
    ``prd-<project>`` snapshot; a partial reference fails closed. Returns the updated fact."""
    return _request("PATCH", f"/candidates/{cid}", body={"meta": meta_dict},
                    space=space, snapshot=snapshot)


def record_outcome(cid: str, success: bool, *, space: str | None = None,
                   snapshot: str | None = None) -> dict:
    """Record a verified build/check outcome on the fact (POST /facts/{cid}/outcome). Pass the
    ticket ``(space, snapshot)`` to record against the mutable ``prd-<project>`` snapshot; a partial
    reference fails closed."""
    return _request("POST", f"/facts/{cid}/outcome", body={"success": bool(success)},
                    space=space, snapshot=snapshot)


def surface_checks(project: str, screen_id: str, scope: str | None = None,
                   space: str | None = None, snapshot: str | None = None) -> list[dict]:
    """Active ``check`` facts bound (via the ``renders`` edge) to surface (project, screen_id).
    ``(space, snapshot)`` bind this read to the project space's checks snapshot (the seam), so a
    surface-bound validation check is resolved from the same snapshot as the tag lane; a partial
    reference fails closed."""
    # screen ids can contain a slash (e.g. "admin/s-login"); encode it so the path segment is valid,
    # and tolerate a 404 (a surface with no checks endpoint must not fail-close the whole resolution —
    # the tag-match lane in resolve_validation_requirements is the authoritative one).
    seg = urllib.parse.quote(screen_id, safe="")
    out = _request("GET", f"/surfaces/{seg}/checks",
                   params={"project": project, "scope": scope}, not_found_ok=True,
                   space=space, snapshot=snapshot)
    return (out or {}).get("checks") or []


def context(query: str, *, top_k: int = 10, as_of: str | None = None,
            space: str | None = None, snapshot: str | None = None) -> list[dict]:
    """Hybrid-ranked (semantic + keyword) retrieval — the SEMANTIC lane for check discovery.
    Returns the ``hits`` list (``{id,text,score,source,scope,category,...}``). ``(space, snapshot)``
    scope the read to the project space's checks snapshot (the seam); a partial reference fails
    closed. An empty query returns no hits (never a blind full-scan)."""
    q = (query or "").strip()
    if not q:
        return []
    out = _request("GET", "/context",
                   params={"query": q, "top_k": top_k, "as_of": as_of},
                   space=space, snapshot=snapshot)
    return out.get("hits") or []


def ping() -> bool:
    """Best-effort liveness check used by smoke tests. Raises PraxisUnreachable if unreachable."""
    _request("GET", "/facts/by", params={"state": "active", "category": "__ping__"})
    return True


# --------------------------------------------------------------------------- preflight

# The ONE reason a factory Stop hook is hard to stand up: two things must be right at once —
# the API must be reachable AND the hook's OWN auth (Cognito refresh token + client id, or an
# API key) must be configured — and when either is missing the gate used to fail closed with a
# GENERIC "check PRAXIS_* / auth" message, then (in headless `claude -p`) loop on the block
# forever. Preflight replaces that with a PRECISE, actionable verdict: it names EXACTLY which of
# PRAXIS_API_BASE_URL / the identity cache / COGNITO_CLIENT_ID / PRAXIS_ORG is missing or failing,
# and classifies the failure as a MISCONFIG (operator error, never self-heals) vs a transient
# UNREACHABLE (server down) so the caller can be loud instead of silently retrying.
#
# It "runs once and caches": the result is memoized to a small file next to the identity cache for
# a few seconds, so a Stop hook firing repeatedly probes Cognito/the API at most once per TTL.

_PREFLIGHT_TTL_S = 30
_MISCONFIG = "misconfig"
_UNREACHABLE = "unreachable"


class PreflightResult(NamedTuple):
    """Structured readiness verdict for the hook's Praxis auth path (see :func:`preflight`)."""

    ok: bool
    kind: str                    # "ok" | "misconfig" | "unreachable"
    org: str
    org_source: str              # "PRAXIS_ORG" | "cache" | "default"
    api_base: str
    failures: tuple[str, ...]    # precise, actionable problems (empty iff ok)
    warnings: tuple[str, ...]    # non-fatal advisories (e.g. falling back to the default org)

    def message(self) -> str:
        """A single human-readable, actionable diagnostic block."""
        where = f"org={self.org} (via {self.org_source}), api={self.api_base}"
        if self.ok:
            head = f"Praxis hook preflight OK — {where}."
            if self.warnings:
                head += "\n" + "\n".join(f"    note: {w}" for w in self.warnings)
            return head
        head = ("Praxis hook is MISCONFIGURED — its auth is not set up, so it can never verify build "
                "state (this will NOT self-heal by retrying)"
                if self.kind == _MISCONFIG else
                "Praxis is UNREACHABLE right now (auth material looks present)")
        lines = "\n".join(f"    - {f}" for f in self.failures)
        note = ("\n" + "\n".join(f"    note: {w}" for w in self.warnings)) if self.warnings else ""
        return f"{head}, {where}:\n{lines}{note}"


def _preflight_cache_file() -> Path:
    return _cache_path().parent / ".hook_preflight.json"


def _preflight_key() -> str:
    """Hash of the config inputs a preflight depends on — a change busts the cache immediately."""
    parts = [
        _api_base(),
        os.environ.get("PRAXIS_ORG", ""),
        str(_cache_path()),
        "key" if os.environ.get("PRAXIS_API_KEY", "").strip() else "",
        os.environ.get("COGNITO_CLIENT_ID", ""),
        os.environ.get("COGNITO_REGION", ""),
        "disabled" if _auth_disabled() else "",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _read_preflight_cache() -> PreflightResult | None:
    try:
        data = json.loads(_preflight_cache_file().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — no/broken cache is just a miss
        return None
    if data.get("key") != _preflight_key():
        return None
    if (time.time() - float(data.get("ts") or 0)) > _PREFLIGHT_TTL_S:
        return None
    try:
        return PreflightResult(
            ok=bool(data["ok"]), kind=str(data["kind"]), org=str(data["org"]),
            org_source=str(data["org_source"]), api_base=str(data["api_base"]),
            failures=tuple(data.get("failures") or ()), warnings=tuple(data.get("warnings") or ()),
        )
    except Exception:  # noqa: BLE001 — malformed cache row is a miss
        return None


def _write_preflight_cache(result: PreflightResult) -> None:
    try:
        path = _preflight_cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "key": _preflight_key(), "ts": time.time(), "ok": result.ok, "kind": result.kind,
            "org": result.org, "org_source": result.org_source, "api_base": result.api_base,
            "failures": list(result.failures), "warnings": list(result.warnings),
        }), encoding="utf-8")
    except Exception:  # noqa: BLE001 — caching is best-effort; never let it crash a gate
        pass


def _run_preflight(*, live: bool) -> PreflightResult:
    api_base = _api_base()
    pinned = os.environ.get("PRAXIS_ORG", "").strip()
    cached_org = _org_from_cache()
    org = _resolve_org(pinned, cached_org, DEFAULT_ORG)
    org_source = "PRAXIS_ORG" if pinned else ("cache" if cached_org else "default")

    failures: list[str] = []
    warnings: list[str] = []
    config_bad = False  # any MISSING-material failure => misconfig (vs a transient live failure)

    if org_source == "default":
        warnings.append(
            f"PRAXIS_ORG is unset and no org is selected in {_cache_path()} — falling back to the "
            f"default org '{DEFAULT_ORG}'. If this project builds under a different org, pin "
            f"PRAXIS_ORG (e.g. in <project>/.claude/settings.local.json) or run praxis_select_org; a "
            f"wrong org resolves an empty ticket set."
        )

    api_key = os.environ.get("PRAXIS_API_KEY", "").strip()
    client_id = os.environ.get("COGNITO_CLIENT_ID", "").strip()
    cache = _cache_path()
    refresh_ok = False

    if _auth_disabled():
        warnings.append("PRAXIS_AUTH_DISABLED=1 — auth is bypassed (dev seam).")
    elif api_key:
        pass  # simplest, complete credential
    else:
        # Cognito refresh-token path: name each missing piece precisely.
        if not cache.exists():
            config_bad = True
            failures.append(
                f"identity cache {cache} is MISSING — the hook mints its Cognito token from the "
                f"refresh token cached there. Create it by logging in via the praxis_login MCP tool, "
                f"OR set PRAXIS_API_KEY, OR point PRAXIS_MCP_CACHE at an existing cache file."
            )
        else:
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                if str(data.get("refresh_token") or "").strip():
                    refresh_ok = True
                else:
                    config_bad = True
                    failures.append(f"identity cache {cache} has no refresh_token — re-run praxis_login.")
            except Exception as exc:  # noqa: BLE001
                config_bad = True
                failures.append(f"identity cache {cache} is unreadable ({exc}) — re-run praxis_login.")
        if not client_id:
            config_bad = True
            failures.append(
                "COGNITO_CLIENT_ID is unset — the hook cannot mint a Cognito token without it. Set "
                "COGNITO_CLIENT_ID (and COGNITO_REGION, default us-east-1) in agent_factory/.env."
            )
        if live and refresh_ok and client_id:
            try:
                _mint_cognito_token()
            except PraxisUnreachable as exc:
                failures.append(
                    f"Cognito token mint FAILED: {exc} — check COGNITO_CLIENT_ID / COGNITO_REGION "
                    f"and network access to cognito-idp."
                )

    # End-to-end reachability: an authenticated probe against the API, only when auth material is
    # sane (a config failure already tells the operator what to fix — no point probing).
    if live and not failures:
        try:
            _request("GET", "/facts/by", params={"state": "active", "category": "__preflight__"})
        except PraxisUnreachable as exc:
            failures.append(
                f"the Praxis API at {api_base} did not answer an authenticated probe: {exc} — is the "
                f"server up? Check PRAXIS_API_BASE_URL (default {DEFAULT_API_BASE})."
            )

    ok = not failures
    kind = "ok" if ok else (_MISCONFIG if config_bad else _UNREACHABLE)
    return PreflightResult(ok=ok, kind=kind, org=org, org_source=org_source, api_base=api_base,
                           failures=tuple(failures), warnings=tuple(warnings))


def preflight(*, live: bool = True, use_cache: bool = True) -> PreflightResult:
    """Fast, PRECISE readiness verdict for the hook's Praxis auth path — the antidote to the silent
    hang. Names exactly which of PRAXIS_API_BASE_URL / the identity cache / COGNITO_CLIENT_ID /
    PRAXIS_ORG is missing or failing, classifies MISCONFIG vs UNREACHABLE, and (by default) memoizes
    the result to disk for ``_PREFLIGHT_TTL_S`` so a looping Stop hook probes at most once per TTL.

    ``live=False`` checks only local config (no Cognito mint, no API call) — cheap and offline.
    ``use_cache=False`` forces a fresh probe (the ``doctor`` command and tests use this).
    """
    if use_cache:
        cached = _read_preflight_cache()
        if cached is not None:
            return cached
    result = _run_preflight(live=live)
    if use_cache:
        _write_preflight_cache(result)
    return result
