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
import sys
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


# The single .env this loader actually read (or None). Exposed so the loader is not SILENT about
# WHICH file — and therefore which backend — won: the whole class of "I edited agent_factory/.env but
# a stale plugin-cache COPY at a different inode is what the hook loaded" bug. See _log_env_resolution.
LOADED_ENV_PATH: Path | None = None


def _load_dotenv() -> Path | None:
    """Load the first existing ``.env`` into ``os.environ`` (without overriding already-set vars).

    A Stop-hook runs as a bare subprocess that does NOT inherit a shell-sourced ``.env``, so the
    factory's Praxis credentials (``PRAXIS_API_KEY``/``PRAXIS_ORG``/...) live in ``<repo>/.env`` and
    must be loaded explicitly. Stdlib-only, tolerant ``KEY=VALUE`` parsing (skips blanks/comments,
    strips optional surrounding quotes). Real environment values WIN over the file, so a per-project
    ``settings.local.json`` env override always beats the shared file. Searched in order: repo root
    (``hooks/..``), cwd, the hooks dir itself; the FIRST one found is authoritative and its path is
    recorded in :data:`LOADED_ENV_PATH`. Returns that path (or ``None`` if no file was found).
    """
    global LOADED_ENV_PATH
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
            LOADED_ENV_PATH = env_path.resolve()
            return LOADED_ENV_PATH
        except Exception:  # noqa: BLE001 — a malformed .env must not crash the gate
            continue
    return None


def _log_env_resolution() -> None:
    """Emit ONE stderr line naming the .env + backend this hook resolved (never silent).

    Suppressed only when ``PRAXIS_HOOK_QUIET=1`` (tests / noise-sensitive runs). This is the
    diagnostic that turns "a copy silently decided which backend was queried" into a visible fact.
    """
    if os.environ.get("PRAXIS_HOOK_QUIET") == "1":
        return
    where = str(LOADED_ENV_PATH) if LOADED_ENV_PATH else "<none: relying on real env vars>"
    backend = os.environ.get("PRAXIS_API_BASE_URL", DEFAULT_API_BASE).rstrip("/")
    print(f"[praxis-hook] env={where} backend={backend}", file=sys.stderr)


_load_dotenv()
_log_env_resolution()


class PraxisUnreachable(RuntimeError):
    """Praxis could not be reached / authenticated / queried. Callers MUST fail closed (BLOCK)."""


class PraxisConflict(PraxisUnreachable):
    """A lease operation lost to a different live owner (HTTP 409).

    A SUBCLASS of :class:`PraxisUnreachable` on purpose: every existing gate catches the
    parent and fails closed, and losing a lease must keep failing closed for them. Only the
    lease helpers that have a meaningful answer to "somebody else holds it" (claim -> return
    False and move to the next ticket; yield -> refuse) catch this narrower type. It is NOT
    an outage: the round-trip succeeded and the server answered a definite no."""


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


def _learnings_credential() -> tuple[str, str] | None:
    """The org + key the SHARED learnings space is read/written under, or ``None`` for "same as
    everything else" (the pre-existing behaviour, and the default).

    Why this exists. Every factory project runs in its OWN Praxis org (``sports-analysis``,
    ``appeal-engine``, ...), each with its own API key, and the server enforces
    ``keyOrg == requestedOrg``. Praxis's sharing primitive (``GET /org/sources``) is explicitly
    INTRA-org — "any member may browse any space's snapshots" means any member *of that org*. So a
    space named ``factory-learnings`` resolved under the ambient org is a DIFFERENT space in every
    project: seven projects, seven isolated stores, and the cross-project learning the factory
    exists to do silently never happens. Verified in the wild — a devbox loop resolved org
    ``sotos`` and reported ``unknown space 'factory-learnings'``.

    Setting ``FACTORY_LEARNINGS_ORG`` (plus ``FACTORY_LEARNINGS_API_KEY``, since the ambient key is
    scoped to the ambient org) points every project's lesson traffic at ONE org, while its tickets,
    checks and plan stay in its own. Unset, nothing changes.
    """
    org = os.environ.get("FACTORY_LEARNINGS_ORG", "").strip()
    if not org:
        return None
    return org, os.environ.get("FACTORY_LEARNINGS_API_KEY", "").strip()


def _auth_headers(*, org_override: str | None = None,
                  key_override: str | None = None) -> dict[str, str]:
    """Build the auth + tenancy headers, failing closed if no credential is available.

    ``org_override`` / ``key_override`` retarget THIS request at a different tenant — used only for
    the shared learnings space (see :func:`_learnings_credential`). They are passed explicitly
    rather than read from the environment here so the override is visible at the one call site that
    applies it, instead of silently rewriting every request's tenancy.
    """
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
    org = org_override or _resolve_org(
        os.environ.get("PRAXIS_ORG", ""), _org_from_cache(), DEFAULT_ORG)
    headers["x-praxis-org"] = org

    if _auth_disabled():
        return headers

    api_key = (key_override or os.environ.get("PRAXIS_API_KEY", "")).strip()
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

    # THE choke point for shared-learnings tenancy. Applied here, on `space`, rather than at the
    # dozens of call sites in `agent_factory.ingestion_api`: a call site that forgot the override
    # would write a lesson into the project's own org, where it is invisible to every other
    # project — the exact silent failure this fixes. There is one door, so nothing can miss it.
    org_override = key_override = None
    if space == FACTORY_LEARNINGS_SPACE and (cred := _learnings_credential()) is not None:
        org_override, key_override = cred
        ambient = _resolve_org(os.environ.get("PRAXIS_ORG", ""), _org_from_cache(), DEFAULT_ORG)
        if not key_override and org_override != ambient:
            # Fail LOUD and precise. Without this the request goes out with the ambient key against
            # a foreign org and comes back 403 "not scoped to org", which `not_a_factory_project`
            # classifies as "no project here" -- i.e. a missing credential would masquerade as a
            # correctly-configured absence, and lessons would silently stop being shared.
            raise PraxisUnreachable(
                f"FACTORY_LEARNINGS_ORG={org_override!r} but no FACTORY_LEARNINGS_API_KEY, and the "
                f"ambient key is scoped to {ambient!r}. A Praxis key only works in its own org, so "
                "the shared learnings space needs its own key. Set FACTORY_LEARNINGS_API_KEY."
            )
    headers = _auth_headers(org_override=org_override, key_override=key_override)
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
        # 409 is the lease endpoints' definite "a different live owner holds this" — a real
        # answer, not an outage. Raised as the PraxisUnreachable SUBCLASS so gates that catch
        # the parent still fail closed, while the lease helpers can tell the two apart.
        cls = PraxisConflict if exc.code == 409 else PraxisUnreachable
        raise cls(f"Praxis {method} {path} -> HTTP {exc.code}: {detail}") from exc
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


def get_fact(cid: str, *, space: str | None = None, snapshot: str | None = None,
             not_found_ok: bool = False) -> dict:
    """Full fact (candidate view) including ``meta``. Raises PraxisUnreachable on any error.
    Pass the ticket ``(space, snapshot)`` to read from a snapshot-bound graph (e.g. the mutable
    ``prd-<project>`` tickets); omit both for working memory. A partial reference fails closed.

    ``not_found_ok`` returns ``{}`` on a benign 404 (the fact simply does not exist) instead of
    fail-closing — the read a planning-marker probe wants, where "no marker fact" means "no planning
    session", NOT "Praxis is down". Every other failure still raises."""
    return _request("GET", f"/candidates/{cid}", space=space, snapshot=snapshot,
                    not_found_ok=not_found_ok)


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


def delete_fact(cid: str, *, space: str | None = None,
                snapshot: str | None = None) -> dict:
    """Hard-delete a fact (DELETE /candidates/{cid}). Pass the ticket ``(space, snapshot)`` to
    target the mutable ``prd-<project>`` snapshot; a partial reference fails closed. Returns
    ``{"deleted": cid}`` on success."""
    return _request("DELETE", f"/candidates/{cid}", space=space, snapshot=snapshot)


def regress_requirements(project: str, ids: list, detail: dict | None = None, *,
                         space: str | None = None, snapshot: str | None = None) -> dict:
    """Regress a SET of tickets so each re-enters the incomplete set (POST /requirements/regress).

    USE THIS, NOT ``patch_meta``, to write build state onto a ticket. A blessed ``prd-<project>``
    plan refuses candidate edits (the S12 bless guard), so ``patch_meta`` fails closed with
    "plan is blessed — re-arm the planning marker" — and re-arming it to record a build outcome
    would unbless the plan as a side effect of building. This endpoint is the sanctioned
    build-state path and is not guarded.

    ``ids`` are FACT ids (cids), never ``requirement_id`` values like "REM-10": passing the latter
    silently returns ``count: 0``, so always resolve the cid first and always check ``count``.
    ``detail`` optionally merges extra meta per id (``{cid: {"regression_detail": ...}}``) —
    the WHY that the next worker's briefing reads back."""
    body = {"project": project, "ids": [str(i) for i in (ids or [])]}
    if detail:
        body["detail"] = detail
    return _request("POST", "/requirements/regress", body=body,
                    space=space, snapshot=snapshot)


def write_build_state(cid: str, meta_dict: dict, *, owner: str | None = None,
                      space: str | None = None, snapshot: str | None = None) -> dict:
    """Write a ticket's BUILD STATE (POST /requirements/{cid}/build-state).

    USE THIS, NOT ``patch_meta``, for anything the build loop LEARNS while executing a plan:
    the coverage contract and pinned checks, a terminal block, the whole-set run marker, a
    swept lease. A blessed ``prd-<project>`` plan refuses candidate edits (the S12 bless
    guard), so ``patch_meta`` fails closed with "plan is blessed — re-arm the planning
    marker" — and re-arming it to record build state would unbless the plan as a side effect
    of BUILDING. This endpoint is the sanctioned build-state path and is not guarded.

    It is not a hole in the guard either: the server accepts ONLY build-lifecycle keys
    (``PostgresVectorGraph.BUILD_STATE_META_KEYS``) and rejects plan content — text, tags,
    surfaces, acceptance, depends_on — with a 400. Those still go through ``patch_meta``
    behind a re-armed planning marker, which is exactly right, because changing them
    changes the PLAN.

    Values REPLACE wholesale (a re-pin TRUNCATES prior per-check state — it is this pass's
    contract, not an append) and ``None`` REMOVES the key. Pass ``owner`` to require that
    owner still holds the lease (raises :class:`PraxisConflict` otherwise); omit it for the
    run-marker writes that legitimately happen before any ticket is claimed.

    ``build_state`` may only be set to ``"blocked"`` here — claim / release / regress own
    the other transitions so their guards cannot be routed around.
    """
    body: dict[str, Any] = {"meta": meta_dict}
    if owner:
        body["owner"] = owner
    out = _request("POST", f"/requirements/{cid}/build-state", body=body,
                   space=space, snapshot=snapshot)
    return out or {}


def claim_requirement(cid: str, owner: str, ttl: int, *, space: str | None = None,
                      snapshot: str | None = None) -> dict | None:
    """Lease ticket ``cid`` to ``owner`` (POST /requirements/{cid}/claim); ``None`` on conflict.

    USE THIS, NOT ``patch_meta``, to take a ticket — see :func:`write_build_state` for why the
    bless guard makes the candidate-edit path unusable on a blessed plan. Beyond dodging the
    guard, the grant here is ATOMIC at the DB row level: two agents racing the same free ticket
    produce exactly one winner, where the read-modify-write it replaces produced two.

    Returns the claim view on grant, or ``None`` when a DIFFERENT owner holds a live lease
    (HTTP 409) — a normal outcome the caller answers by moving to the next ticket, not an
    outage. Every other failure still raises :class:`PraxisUnreachable`."""
    try:
        out = _request("POST", f"/requirements/{cid}/claim",
                       body={"owner": owner, "lease_ttl_seconds": int(ttl)},
                       space=space, snapshot=snapshot)
    except PraxisConflict:
        return None
    return (out or {}).get("claim") or {}


def release_requirement(cid: str, owner: str, state: str, *, honor_takeover: bool = False,
                        space: str | None = None, snapshot: str | None = None) -> dict | None:
    """Release ``owner``'s lease with a terminal ``state`` (POST /requirements/{cid}/release).

    USE THIS, NOT ``patch_meta``, to finish or yield a ticket — see :func:`write_build_state`
    for why the bless guard makes the candidate-edit path unusable on a blessed plan. It is
    also the chokepoint that refuses to finish a ticket nothing gates (empty ``pinned_checks``
    with no ``meta.checks_waived_reason``), which is why RESOLVE's pin must land BEFORE the
    finish, and it stamps the server-owned ``finished_at``.

    ``honor_takeover`` (``finished`` only) records the completion even if the lease has since
    moved on — completion is a fact about the world, not about who holds a lease.

    Returns the claim view, or ``None`` when the release was refused for lease reasons
    (HTTP 409). Every other failure raises :class:`PraxisUnreachable`."""
    body: dict[str, Any] = {"owner": owner, "state": state}
    if honor_takeover:
        body["honor_takeover"] = True
    try:
        out = _request("POST", f"/requirements/{cid}/release", body=body,
                       space=space, snapshot=snapshot)
    except PraxisConflict:
        return None
    return (out or {}).get("claim") or {}


def record_outcome(cid: str, success: bool, *, space: str | None = None,
                   snapshot: str | None = None) -> dict:
    """Record a verified build/check outcome on the fact (POST /facts/{cid}/outcome). Pass the
    ticket ``(space, snapshot)`` to record against the mutable ``prd-<project>`` snapshot; a partial
    reference fails closed."""
    return _request("POST", f"/facts/{cid}/outcome", body={"success": bool(success)},
                    space=space, snapshot=snapshot)


def ensure_planning_marker(project: str, *, category: str | None = None,
                           space: str | None = None,
                           snapshot: str | None = None) -> str:
    """Idempotently ensure ``project``'s marker fact exists; return its id.

    The BOOTSTRAP for the ``plan_completeness`` arming signal: nothing else creates the marker, so
    on a greenfield project ``stamp_planning`` has no fact to write session meta onto. Find-or-create
    happens server-side (see ``ensure_planning_marker`` in the graph), which also makes it race-free
    across concurrent intakes. Pass the plan ``(space, snapshot)`` so the marker lands in
    ``prd-<project>`` where the hook reads it.

    The optional ``category`` parameter (defaults to ``"planning-marker"`` on the server) allows
    markers of different kinds to coexist per project without overwriting one another."""
    body = {"project": project}
    if category is not None:
        body["category"] = category
    out = _request("POST", "/planning-marker", body=body,
                   space=space, snapshot=snapshot)
    return str((out or {}).get("id") or "")


def ensure_build_marker(project: str, *, space: str | None = None,
                        snapshot: str | None = None) -> str:
    """Idempotently ensure ``project``'s build-marker fact exists; return its id.

    The marker holds gate-disable state for the factory's Stop hooks — when a gate stands
    down because a disable variable is set, the variable name and observed value are
    recorded here. Bootstrap on a greenfield project. Pass the plan ``(space, snapshot)``
    so the marker lands in ``prd-<project>`` where the hooks write/read it."""
    out = _request("POST", "/build-marker", body={"project": project},
                   space=space, snapshot=snapshot)
    return str((out or {}).get("id") or "")


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


# --------------------------------------------------------------------------- episodes / contradictions

# The decision-log + contradiction lanes the MCP tools (``praxis_record_episode`` /
# ``praxis_get_contradictions``, ``knowledge/mcp/server.py:684,745``) call, exposed here so a
# Stop-hook subprocess and ``tools/plan_gate_check.py`` can read/write them WITHOUT importing the
# praxis package. Episodes are store-only decision journals (``category="episodic"``, out of semantic
# recall); the signed-contract episode rides here as a ``meta.episode`` payload (see
# ``src/agent_factory/contract_signature.py`` for the pure payload/validation helpers).

def record_episode(text: str, *, episode: dict | None = None,
                   alternatives: list[str] | None = None, outcome: str = "pending",
                   derived_from: list[str] | None = None, decided_at: str | None = None,
                   space: str | None = None, snapshot: str | None = None) -> dict:
    """Record a decision-log episode (POST /insights, ``category="episodic"``) — mirror of the
    ``praxis_record_episode`` MCP tool's request body. ``episode`` is the full ``meta.episode``
    payload (e.g. the signed-contract payload from ``contract_signature.build_signed_payload``);
    ``outcome``/``alternatives``/``decided_at`` fill in the store-only fields the tool also sets.
    Raises PraxisUnreachable on any error (fail-closed, like the rest of the client)."""
    payload = dict(episode or {})
    payload.setdefault("outcome", outcome)
    if alternatives:
        payload["alternatives"] = list(alternatives)
    if decided_at is not None:
        payload["decided_at"] = decided_at
    body: dict[str, Any] = {"insight": text, "category": "episodic",
                            "meta": {"episode": payload}}
    if derived_from:
        body["derivedFrom"] = list(derived_from)
    return _request("POST", "/insights", body=body, space=space, snapshot=snapshot)


def get_episodes(*, meta: dict | None = None, space: str | None = None,
                 snapshot: str | None = None) -> list[dict]:
    """EXHAUSTIVE enumeration of the ``category="episodic"`` decision-log facts (GET /facts/by), so a
    gate can find the signed-contract episode and validate it. ``meta`` is a flat top-level meta
    filter (scalar-equality OR array-membership). ``(space, snapshot)`` bind the read to a
    snapshot-bound graph; a partial reference fails closed (see :func:`_request`). Empty -> ``[]``."""
    return facts_by(category="episodic", meta=meta, space=space, snapshot=snapshot)


def get_contradictions(*, space: str | None = None, snapshot: str | None = None) -> list[dict]:
    """The flagged contradiction clusters (GET /contradictions) — mirror of ``praxis_get_contradictions``.
    Pass BOTH ``space`` and ``snapshot`` to review contradictions raised INSIDE an org-shared snapshot
    (e.g. an ``on_conflict="surface"`` clash from authoring a NON-TICKET planning fact into the plan;
    TICKET writes never reach the contradiction step at all — they are identity-keyed on
    ``meta.requirement_id`` and carry no ``on_conflict``); omit both for working memory.
    Returns the cluster list (``[]`` when none flagged). Raises PraxisUnreachable on any error."""
    out = _request("GET", "/contradictions", space=space, snapshot=snapshot)
    if isinstance(out, list):
        return out
    return out.get("contradictions") or out.get("clusters") or []


def ping() -> bool:
    """Best-effort liveness check used by smoke tests. Raises PraxisUnreachable if unreachable."""
    _request("GET", "/facts/by", params={"state": "active", "category": "__ping__"})
    return True


# --------------------------------------------------------------------------- mounts (read-only overlays)

# The org-level shared learnings space (FL1 / KD1): lessons and the failure-class taxonomy live here,
# cloud-canonical, and get mounted read-only into every project's working memory. This is the ONE place
# the (space, snapshot) pair is named so every caller — the mount-at-claim-time call below and
# ``agent_factory.ingestion_api``, the sole writer — agrees on where "the shared learnings space" is.
FACTORY_LEARNINGS_SPACE = "factory-learnings"
FACTORY_LEARNINGS_SNAPSHOT = "lessons"
# Proof-artifact bundles (FL4 / R7) live in the SAME shared space under their own snapshot — never
# mounted read-only alongside lessons (only an explicit ``mount_snapshot(space, "artifacts")`` call
# would expose them), which keeps the cross-project-readability question D3 leaves open from being
# decided by accident.
FACTORY_ARTIFACTS_SNAPSHOT = "artifacts"
# Push-not-pull pending-attention flags (FL18 / R24) — suspension/parking/undraftable/check-defeat
# events — live in the SAME shared space under their own snapshot, org-wide so `af-retro --flags`
# aggregates across every project from one place.
FACTORY_FLAGS_SNAPSHOT = "flags"
# Cloud-promoted universal checks (FL14 / R14, D8) — the dual-source seam's cloud half: a check
# promoted after recurrence in >=2 distinct projects lives here, org-wide, so it resolves for every
# project (including one that never saw the originating failure) alongside seeded_checks.toml's
# git-shipped universals, in the same read pass.
FACTORY_PROMOTED_UNIVERSALS_SNAPSHOT = "promoted-universals"


def mount_snapshot(space: str, snapshot: str, *, not_found_ok: bool = False) -> dict[str, Any]:
    """Mount ``(space, snapshot)`` as a READ-ONLY overlay on the caller's own working memory
    (POST /mounts). A mounted overlay is retrieval-only: it widens what ``context``/``facts_by`` see
    for the authenticated caller, but there is no write endpoint that targets an overlay — writing
    into the mounted space/snapshot itself requires an explicit ``space=``/``snapshot=`` write call
    against it, which nothing but the owning writer (e.g. ``ingestion_api`` for the learnings space)
    is meant to issue. Idempotent: mounting an already-mounted pair is a no-op.

    The server refuses to mount a snapshot with zero rows (HTTP 404) — an empty shared space is the
    legitimate starting state (e.g. before the first lesson is ever ingested), not an outage. Pass
    ``not_found_ok=True`` to treat that specific case as a benign no-op (``{}``) instead of raising;
    every other failure still raises :class:`PraxisUnreachable`."""
    return _request("POST", "/mounts",
                    body={"space": _require_str(space, "space"),
                          "snapshot": _require_str(snapshot, "snapshot")},
                    not_found_ok=not_found_ok)


def _require_str(value: str, name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def ensure_space(space_id: str, name: str | None = None) -> str:
    """Idempotently ensure org-shared ``space_id`` exists (POST /spaces); return it.

    A snapshot-bound write into a space that has never been created 404s (``_require_space`` on
    the server), so this is the one-time bootstrap a fresh space's first writer needs. A 409
    ("already exists") is exactly the steady-state case after the first call and is swallowed,
    not raised — every other failure still raises :class:`PraxisUnreachable`."""
    sid = _require_str(space_id, "space_id")
    try:
        _request("POST", "/spaces", body={"spaceId": sid, "name": name})
    except PraxisUnreachable as exc:
        if "HTTP 409" not in str(exc):
            raise
    return sid


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


# --------------------------------------------------------------------------- whoami

class WhoAmI(NamedTuple):
    """The resolved identity for THIS invocation — the crisp one-line answer to
    "who am I, against which backend, in which org?" (see :func:`whoami`)."""

    backend: str
    org: str
    org_source: str          # "PRAXIS_ORG" | "cache" | "default"
    principal: str           # server-reported sub (or "?" if unreachable)
    auth_mode: str           # "key" | "bearer" | "dev" | "?"
    key_org: str | None      # the org a key is scoped to (None for bearer/dev)
    ok: bool
    detail: str              # crisp mismatch/error when not ok (else "")

    def line(self) -> str:
        """The single diagnostic line: identity + a MISMATCH clause when broken."""
        base = (
            f"backend={self.backend} resolved_org={self.org} (via {self.org_source}) "
            f"principal={self.principal} auth_mode={self.auth_mode}"
        )
        if self.key_org is not None:
            base += f" key_org={self.key_org}"
        return base if self.ok else f"{base}  MISMATCH: {self.detail}"


def whoami() -> WhoAmI:
    """Resolve and return THIS invocation's identity by asking the server ``GET /whoami``.

    Sends the exact auth + ``x-praxis-org`` headers a real hook request sends (so it reports the
    truth the gates see), then compares the key's org against the resolved org to surface the
    canonical multi-tenancy footgun as one line: "key scoped to org 'sotos' but PRAXIS_ORG='bestie'".
    """
    backend = _api_base()
    pinned = os.environ.get("PRAXIS_ORG", "").strip()
    cached = _org_from_cache()
    org = _resolve_org(pinned, cached, DEFAULT_ORG)
    org_source = "PRAXIS_ORG" if pinned else ("cache" if cached else "default")

    try:
        data = _request("GET", "/whoami")
    except PraxisUnreachable as exc:
        return WhoAmI(backend, org, org_source, "?", "?", None, False,
                      f"cannot reach {backend}/whoami: {exc}")

    principal = str(data.get("sub") or "?")
    auth_mode = str(data.get("authMode") or "?")
    key_org = data.get("keyOrg")
    ok = bool(data.get("orgMatch", True))
    detail = ""
    if not ok:
        if auth_mode == "key":
            src = f"PRAXIS_ORG={org!r}" if org_source == "PRAXIS_ORG" else f"resolved org {org!r}"
            detail = f"key scoped to org {key_org!r} but {src} (via {org_source})"
        else:
            detail = str(data.get("detail") or f"{auth_mode} principal not a member of org {org!r}")
    return WhoAmI(backend, org, org_source, principal, auth_mode, key_org, ok, detail)
