"""The ``praxis-knowledge`` MCP server: thin tools over the backend's HTTP API.

Each tool is a thin authenticated client that calls the backend with
``X-Praxis-Org: <org>`` plus ONE credential, resolved by this precedence (see
:func:`_headers`): the ``PRAXIS_MCP_AUTH_DISABLED`` dev seam, else a durable
org-scoped ``pxk_`` key from ``PRAXIS_API_KEY`` (``X-Praxis-Key`` — no login
needed, survives restarts; parity with the af-build hook), else a fresh Cognito
ID token minted from the cached login (:mod:`knowledge.mcp.identity`,
``Authorization: Bearer <token>``). Tenancy and the ingestion/retrieval pipeline
live entirely on the backend; nothing here touches the database.

Login happens through the MCP tools themselves (``praxis_login`` / org tools), so
the only setup is registering the server — no separate CLI step:

    claude mcp add praxis -- uv run python -m knowledge.mcp

Then, in a session, ask Claude to log you in (it calls ``praxis_login``).
"""

from __future__ import annotations

import json
import os
import re

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from knowledge.mcp import identity

mcp = FastMCP("praxis-knowledge")

# httpx's default per-request timeout (5s) is too low for the write path. A write
# whose new fact is a cosine-near-neighbor of an existing one triggers the inline
# SemanticConflictDetector — a synchronous LLM round-trip (plus embedding) inside
# the request — which can push total latency well past 5s. The backend still
# commits, but the client gives up first and surfaces a spurious "timed out".
# So: a short budget for reads (keeps /context, /health snappy) and a long one
# for writes/ingest (the conflict-checked path). Per-call, not one global bump.
_READ_TIMEOUT = 30.0
_WRITE_TIMEOUT = 120.0

_AUTH_HINT = (
    "authentication failed — ask me to log in again with `praxis_login`, or check "
    "you are a member of the active org."
)


def _auth_disabled() -> bool:
    """Local dev seam: skip the Cognito login gate for an auth-disabled backend.

    Gated on ``PRAXIS_MCP_AUTH_DISABLED=1`` — deliberately distinct from the
    backend's ``PRAXIS_AUTH_DISABLED`` (which the test harness sets process-wide),
    so this client bypass never activates unintentionally. When set, the MCP client
    sends no bearer token (the auth-disabled backend ignores it and uses its fixed
    ``dev-user`` principal), so no login or Cognito config is needed. The data tools
    just need an org the dev principal belongs to — see ``_dev_org``.
    """
    return os.environ.get("PRAXIS_MCP_AUTH_DISABLED") == "1"


def _dev_org() -> str:
    """The ``X-Praxis-Org`` to send in auth-disabled mode.

    The backend still authorizes org membership (the dev principal must be a member
    of this org). Override with ``PRAXIS_MCP_ORG``; defaults to ``"default"``.
    """
    return os.environ.get("PRAXIS_MCP_ORG", "default").strip() or "default"


def _api_key() -> str:
    """A durable, org-scoped ``pxk_`` key from ``PRAXIS_API_KEY`` (``""`` if unset).

    When set, the MCP tools authenticate to a specific org with this ONE key —
    the same durable credential the af-build Stop-hook uses (``hooks/_praxis``),
    and the simplest way to pin the MCP tools to an org WITHOUT a Cognito login (no
    refresh-token cache, survives restarts). It takes precedence over the bearer
    mint, mirroring the hook's precedence exactly. The key IS org-scoped, so the
    org sent alongside it must be the key's org (see :func:`_key_org`).
    """
    return os.environ.get("PRAXIS_API_KEY", "").strip()


def _key_org() -> str:
    """The org to send with an API key: the ``PRAXIS_ORG`` pin, else the cached selection.

    A key is org-scoped and the backend 403s a mismatch, so this MUST resolve — a
    key with no org is a misconfiguration, not a default. Does not require a login
    (the whole point of key auth), but honors a cached ``praxis_select_org`` if one
    happens to exist. Raises a precise hint when neither is set.
    """
    org = identity.pinned_org()
    if not org:
        try:
            org = identity.load_identity().org_id
        except Exception:  # noqa: BLE001 — no/unreadable cache is fine; the pin is the real source
            org = ""
    if not org:
        raise RuntimeError(
            "PRAXIS_API_KEY is set but no org is selected — pin PRAXIS_ORG (the key's org) in this "
            "project's .claude/settings.local.json, or log in and run praxis_select_org."
        )
    return org


def _headers(space: str | None = None, snapshot: str | None = None) -> dict[str, str]:
    # Auth + org. With NO (space, snapshot) the request resolves to the authenticated
    # principal's working memory (the default for personal-knowledge ops). Passing BOTH
    # ``space`` and ``snapshot`` emits the ``X-Praxis-Space``/``X-Praxis-Snapshot`` headers
    # so the op targets that ORG-SHARED snapshot graph instead — the seam the factory uses
    # to author checks into ``building-validation``/``planning-validation`` and to read/write
    # ``prd-<project>`` ticket state, exactly where the af hooks read. A partial reference
    # (exactly one of space/snapshot) is a misconfiguration and RAISES (fail-closed, mirroring
    # hooks/_praxis) rather than silently falling back to working memory.
    if (space is None) != (snapshot is None):
        raise ValueError(
            f"space and snapshot must both be given or both omitted "
            f"(space={space!r}, snapshot={snapshot!r})"
        )
    if _auth_disabled():
        # No bearer: the auth-disabled backend ignores it and uses dev-user.
        headers = {"X-Praxis-Org": _dev_org()}
    elif _api_key():
        # Durable, org-scoped key — the preferred credential (parity with the hook).
        headers = {"X-Praxis-Key": _api_key(), "X-Praxis-Org": _key_org()}
    else:
        headers = {
            "Authorization": f"Bearer {identity.token()}",
            "X-Praxis-Org": identity.active_org(),
        }
    if space is not None:
        headers["X-Praxis-Space"] = space
        headers["X-Praxis-Snapshot"] = snapshot  # type: ignore[assignment]
    return headers


def _resolve_space(space: str | None) -> str:
    """The explicit ``space`` arg, else the local client default (``praxis_select_space``).

    ``praxis_select_space`` sets a purely client-side default that feeds the ``space``
    parameter of the snapshot / mount / space ops — it is NOT a header and never
    selects a working graph. An explicit ``space`` argument always wins.
    """
    if space and space.strip():
        return space.strip()
    return identity.active_space()


def _normalize_tag(tag: object) -> str:
    """Canonicalize ONE applicability tag: ``strip().casefold()``, preserving the ``"*"`` wildcard.

    This is the AUTHOR-TIME mirror of ``agent_factory/hooks/_ticket_state.py:normalize_tag`` — the
    factory's check↔ticket predicate is a server-side EXACT array-membership match, so a check
    authored ``applies_to:["Auth"]`` would silently never pin onto a ticket tagged ``["auth"]``.
    Normalizing both sides at write time (here) AND at resolve time (the hook) removes that footgun.
    The hook subprocess is stdlib-only and cannot import this module, so the two functions are kept
    byte-for-byte equivalent and an agent_factory test asserts they agree — keep them in lockstep.
    """
    s = str(tag).strip()
    return s if s == "*" else s.casefold()


def _normalize_applicability(meta: dict | None) -> dict | None:
    """Return ``meta`` with its applicability lanes (``applies_to`` on a check, ``tags`` on a ticket)
    tag-normalized, so authored facts match the way :func:`resolve_validation_requirements` queries.

    Only those two keys are touched (each may be a scalar or a list); every other meta value is passed
    through untouched. Empty/blank tags are dropped and duplicates collapsed (order preserved). A
    ``meta`` with neither key — the common case — is returned unchanged.
    """
    if not isinstance(meta, dict):
        return meta
    out = dict(meta)
    for key in ("applies_to", "tags"):
        if key not in out:
            continue
        raw = out[key]
        values = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        seen: list[str] = []
        for v in values:
            if v is None:
                continue
            norm = _normalize_tag(v)
            if norm and norm not in seen:
                seen.append(norm)
        out[key] = seen if isinstance(raw, (list, tuple)) else (seen[0] if seen else raw)
    return out


def _friendly(exc: httpx.HTTPStatusError) -> str:
    """Turn an HTTP error into a message the caller can actually act on.

    401/403 keep the login hint (nothing in the body helps there). Everything else
    used to RE-RAISE, which reached the agent as a bare transport error — a 400 from
    ``PATCH /candidates/{cid}`` would say "Client error '400 Bad Request'" and drop the
    one thing that tells you how to fix it (e.g. "plan 'prd-sotos' is blessed — re-arm
    the planning marker (stamp_planning) to mutate this snapshot"). So: surface the
    body's ``detail``, falling back to the raw body text, then to the reason phrase,
    alongside the status code and the URL path. The body may not be JSON (proxy HTML,
    empty 502) — every lookup is defensive and can only degrade the message, never raise.
    """
    resp = exc.response
    status = resp.status_code
    if status in (401, 403):
        return _AUTH_HINT
    detail = ""
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 — non-JSON body (proxy HTML, empty) is expected
        payload = None
    if isinstance(payload, dict):
        raw = payload.get("detail", payload.get("message", payload.get("error", "")))
        detail = raw if isinstance(raw, str) else json.dumps(raw) if raw else ""
    elif isinstance(payload, str):
        detail = payload
    if not detail:
        try:
            detail = (resp.text or "").strip()
        except Exception:  # noqa: BLE001 — undecodable body; fall through to the reason
            detail = ""
    if not detail:
        detail = resp.reason_phrase or "no detail returned"
    if len(detail) > 1200:
        detail = detail[:1200] + "… (truncated)"
    path = ""
    try:
        path = resp.request.url.path
    except Exception:  # noqa: BLE001 — no request attached (hand-built response in tests)
        path = str(getattr(resp, "url", "") or "")
    where = f" on {path}" if path else ""
    return f"Praxis backend returned {status}{where}: {detail}"


def _resolve_requirement_cid(
    requirement_id: str, space: str | None, snapshot: str | None
) -> tuple[str | None, str | None]:
    """Resolve ONE ``meta.requirement_id`` to a fact id within ``(space, snapshot)``.

    Returns ``(cid, None)`` on an unambiguous hit, else ``(None, message)``. Agents know a
    ticket as "R7", not as a 32-hex uuid, and fumbling that uuid is how the wrong row gets
    mutated — so the lifecycle tools accept the identity directly and resolve it here.

    Deliberately strict:
    * ``state="any"`` — a rejected/superseded twin still occupies the requirement_id and is
      precisely what you are usually trying to delete, so it must be visible here.
    * NO ``category`` filter — a fact corrupted by a bad merge can have a NULL or wrong
      category, and pinning ``category="requirement"`` would hide the very rows we chase.
    * MORE THAN ONE match is an ERROR, never a tiebreak: two facts sharing one
      requirement_id IS the corruption signature. We name every id and state and let the
      caller decide which one dies, addressing it by cid.
    * ``(space, snapshot)`` is the search scope and is required — resolving an identity
      against working memory when the ticket lives in ``prd-<project>`` would silently
      find nothing (or, worse, someone else's like-named row).
    """
    if space is None or snapshot is None:
        return None, (
            f"requirement_id={requirement_id!r} needs BOTH space and snapshot — that pair is "
            "the search scope (e.g. space=<project>, snapshot='prd-<project>'). "
            "Pass a cid instead to act on a working-memory fact."
        )
    try:
        resp = httpx.get(
            f"{identity.api_base()}/facts/by",
            params={"state": "any", "meta": json.dumps({"requirement_id": requirement_id})},
            headers=_headers(space, snapshot),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return None, _friendly(exc)
    facts = resp.json().get("facts", []) or []
    if not facts:
        return None, (
            f"No fact carries meta.requirement_id={requirement_id!r} in "
            f"(space={space!r}, snapshot={snapshot!r}) in any state — list what is there with "
            "praxis_facts_by(state='any', space=..., snapshot=...)."
        )
    if len(facts) > 1:
        listing = "; ".join(
            f"{f.get('id')} (state={f.get('state')}, category={f.get('category')!r})"
            for f in facts
        )
        return None, (
            f"AMBIGUOUS: {len(facts)} facts carry meta.requirement_id={requirement_id!r} in "
            f"(space={space!r}, snapshot={snapshot!r}) — refusing to guess. Matches: {listing}. "
            "Two facts sharing one requirement_id is itself a corruption signature (a rejected "
            "twin, or a merge that minted a duplicate) — inspect them with praxis_get_fact and "
            "delete the wrong one by its cid."
        )
    return str(facts[0].get("id")), None


def _timeout_note(what: str) -> str:
    """A clearer message than a bare 'timed out' for a write that may have committed."""
    return (
        f"The {what} request exceeded the client timeout ({int(_WRITE_TIMEOUT)}s). "
        "The write may still have committed on the backend — read it back with "
        "praxis_list_graph / praxis_get_context before retrying to avoid a duplicate."
    )


def _not_ready() -> str | None:
    """A guidance string when we can't call the backend yet, else ``None``.

    Lets the data tools fail soft (telling Claude how to get the user logged in /
    an org selected) instead of raising, so login is fully chat-driven.
    """
    if _auth_disabled():
        return None
    if not identity.is_logged_in():
        return (
            "Not logged in to Praxis. Ask the user for their Praxis email and "
            "password, then call `praxis_login`."
        )
    if not identity.active_org():
        try:
            orgs = identity.list_my_orgs()
        except Exception:  # noqa: BLE001 - token/network issue surfaces as login hint
            return "Not logged in to Praxis — call `praxis_login` again."
        listing = ", ".join(identity.org_id_of(o) for o in orgs) or "(none)"
        return (
            "Logged in, but no active org is selected. Your orgs: "
            f"{listing}. Call `praxis_select_org` (or `praxis_create_org` / "
            "`praxis_join_org`)."
        )
    return None


def _structured(summary: str, data: dict) -> str:
    """A consumable result: a human summary line plus a fenced JSON block.

    The external agent parses the ```json fence; humans read the first line. Kept
    as a single string so it matches the other tools' ``-> str`` convention.
    """
    return f"{summary}\n\n```json\n{json.dumps(data, indent=2)}\n```"


@mcp.tool()
def praxis_get_context(
    query: str,
    top_k: int = 8,
    include_episodic: bool = False,
    as_of: str | None = None,
    category: str | None = None,
    categories: list[str] | None = None,
    scope: str | None = None,
    meta_filter: dict | None = None,
) -> str:
    """Retrieve relevant stored knowledge for the current task.

    Call this before answering questions about the user's preferences,
    conventions, or past decisions — it returns active facts from the user's
    knowledge graph most similar to ``query``.

    Returns a human summary plus a structured JSON block with ``context`` and
    per-hit ``hits`` (each with ``id``/``text``/``score``/``source``/``scope``/
    ``category``) so callers can consume provenance without regex-parsing. If you
    have mounted snapshots (``praxis_mount_snapshot``), their facts are included
    too and flagged with ``mounted``/``owner``/``snapshot`` on the hit.

    Episodic decision logs (``category="episodic"``) are excluded by default (H2)
    so "why we decided" notes never pollute recall; pass ``include_episodic=True``
    to include them. ``as_of`` (an ISO-8601 timestamp, e.g. ``2024-01-01T00:00:00Z``)
    rewinds retrieval to that instant — facts written later are excluded — for
    point-in-time recall.

    Optional POSITIVE filters narrow the similarity-ranked results to a subset
    (still ranked by relevance, not exhaustive — use ``praxis_facts_by`` for an
    exhaustive enumeration): ``category`` (single) and/or ``categories`` (a list)
    keep only those categories; ``scope`` matches the top-level scope; ``meta_filter``
    is a ``{key: value}`` object matched against the JSONB ``meta`` (scalar equality
    OR array-membership) — e.g. category="check" with meta_filter={"scope":"planning"}
    returns the planning checks most similar to ``query``. Filters apply to live and
    mounted facts alike.
    """
    if (hint := _not_ready()) is not None:
        return hint
    params: dict[str, object] = {"query": query, "top_k": top_k}
    if include_episodic:
        params["include_episodic"] = True
    if as_of is not None:
        params["as_of"] = as_of
    if category:
        params["category"] = category
    if categories:
        params["categories"] = ",".join(categories)
    if scope:
        params["scope"] = scope
    if meta_filter:
        params["meta"] = json.dumps(meta_filter)
    try:
        resp = httpx.get(
            f"{identity.api_base()}/context",
            params=params,
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    hits = payload.get("hits", [])
    return _structured(
        payload.get("context", "") or f"{len(hits)} hit(s).",
        {"context": payload.get("context", ""), "hits": hits},
    )


@mcp.tool()
def praxis_get_stale_derivations() -> str:
    """List learnings flagged stale because a fact they derive from was invalidated (H5).

    When a source fact is invalidated (e.g. rejected via ``praxis_reject_fact``),
    Praxis flags every learning transitively derived from it for review — it does
    NOT auto-reject them (precision-first). Call this to surface those suspect
    learnings, then confirm with the user before re-checking or rejecting each.

    Returns a human summary plus a structured JSON block with ``stale`` — one entry
    per flagged learning (``id``/``text``/``state``/``source``/``scope``/
    ``category``/``meta``).
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/derivations/stale",
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    stale = payload.get("stale", [])
    return _structured(
        f"{len(stale)} stale derived learning(s) flagged for review."
        if stale
        else "No stale derived learnings are currently flagged.",
        {"stale": stale},
    )


@mcp.tool()
def praxis_dependents(fact_id: str) -> str:
    """List the learnings transitively derived from ``fact_id`` (its dependents).

    Walks the ``derived_from`` chain to find every learning that depends on this
    fact, so you can see what would be affected if it changed or were invalidated.
    Find the id via ``praxis_list_graph`` / ``praxis_get_context``.

    Returns a human summary plus a structured JSON block with ``dependents`` — one
    entry per dependent learning (``id``/``text``/``state``/``source``/``scope``/
    ``category``/``meta``).
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/facts/{fact_id}/dependents",
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    deps = payload.get("dependents", [])
    return _structured(
        f"{len(deps)} learning(s) derive from {fact_id}."
        if deps
        else f"No learnings derive from {fact_id}.",
        {"factId": fact_id, "dependents": deps},
    )


@mcp.tool()
def praxis_list_jobs() -> str:
    """List live box-service jobs and their states (R26), ordered so every job needing
    attention — ``awaiting-human``, ``failed``, or silently past the silence threshold —
    sorts above every job progressing normally. The website's top-level jobs list reads
    the identical ordering from the same backend endpoint
    (``knowledge/serve/box_service_jobs_view.order_jobs_for_view``), so an operator gets
    the same answer from either surface.

    Returns a human summary plus a structured JSON block with ``jobs`` — one entry per
    job (``id``/``state``/``needsAttention``/...), already in display order.
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/jobs",
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    jobs = payload.get("jobs", [])
    attention = sum(1 for j in jobs if j.get("needsAttention"))
    return _structured(
        f"{len(jobs)} job(s), {attention} needing attention."
        if jobs
        else "No live jobs.",
        {"jobs": jobs},
    )


@mcp.tool()
def praxis_job_activity(job_id: str) -> str:
    """Fetch one job's recent activity (R26) — the per-job view of what it has been
    doing, e.g. for a mix of progressing and attention-needing jobs surfaced by
    ``praxis_list_jobs``. Backed by the bounded rolling activity tail (R25), so recent
    messages remain readable even after the job's session is gone.

    Returns a human summary plus a structured JSON block with ``jobId``/``activity``.
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/jobs/{job_id}/activity",
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"Unknown job {job_id} — list ids with praxis_list_jobs."
        return _friendly(exc)
    payload = resp.json()
    activity = payload.get("activity", "")
    return _structured(
        f"activity for job {job_id} ({len(activity)} chars)."
        if activity
        else f"No recorded activity for job {job_id}.",
        {"jobId": job_id, "activity": activity},
    )


@mcp.tool()
def praxis_get_job(job_id: str) -> str:
    """Fetch one job's full detail (R89), including which model backend (sonnet or
    deepseek) was active when the job's session was launched — the per-job counterpart
    to ``praxis_list_jobs``. The website's per-job detail reads the same backend
    endpoint (``GET /jobs/{job_id}``), so an operator gets the same answer from either
    surface.

    Returns a human summary plus a structured JSON block with the job's ``id``,
    ``state``, ``modelBackend``, and any state-specific fields (``branch``/``prUrl``
    for a completed job, ``failureReason``/``commandOutput`` for a failed one).
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/jobs/{job_id}",
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"Unknown job {job_id} — list ids with praxis_list_jobs."
        return _friendly(exc)
    payload = resp.json()
    backend = payload.get("modelBackend", "unknown")
    return _structured(
        f"Job {job_id}: state={payload.get('state','?')}, backend={backend}.",
        payload,
    )


@mcp.tool()
def praxis_view_backend() -> str:
    """View the box's currently-active model backend (sonnet or deepseek) (R88).

    Any authenticated operator in the box's org may view it — org membership is
    the only gate for reads, mirroring the read-authorisation pattern used for
    job listings.

    Returns a human summary and a ``{"backend": "sonnet"|"deepseek"}`` block.
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/backends/active",
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    backend = payload.get("backend", "")
    return _structured(
        f"Active model backend: {backend}.",
        payload,
    )


@mcp.tool()
def praxis_switch_backend(backend: str) -> str:
    """Switch the box's active model backend to ``backend`` (``"sonnet"`` or
    ``"deepseek"``) (R88).

    Only an authenticated operator in the box's org may switch — mirroring the
    operator-scoped authorisation used for job-control actions (resume, cancel).
    Takes effect for sessions launched *after* the call; sessions already
    running are never interrupted.  Persists the choice the same machine-wide
    way ``af-backend`` does, with the same exclusivity guarantee (only the
    selected backend's credential is exposed to launched sessions).

    Pass ``"sonnet"`` or ``"deepseek"``.  Returns the new active backend on
    success.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if not backend or not backend.strip():
        return "Pass a non-empty backend ('sonnet' or 'deepseek')."
    try:
        resp = httpx.put(
            f"{identity.api_base()}/backends/active",
            json={"backend": backend.strip()},
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400:
            return f"Invalid backend: {exc.response.text}"
        return _friendly(exc)
    payload = resp.json()
    return _structured(
        f"Active model backend switched to {payload.get('backend', '')}.",
        payload,
    )


@mcp.tool()
def praxis_get_fact(cid: str, space: str | None = None, snapshot: str | None = None) -> str:
    """Fetch one fact's full detail, including its writer-supplied ``meta``.

    ``praxis_get_context`` hits carry ``source``/``scope``/``category`` but not the
    free-form ``meta`` object (kept off the lean recall path). Use this to read a
    fact's ``meta`` (e.g. ``{"requirement_id": "R4"}``) and full audit trail back.
    Find the id via ``praxis_list_graph`` / ``praxis_get_context``.

    Pass BOTH ``space`` and ``snapshot`` to read a fact from an org-shared snapshot (e.g. a
    check in ``building-validation``, a ticket in ``prd-<project>``); omit both for working
    memory. A fact written to a snapshot is NOT in working memory, so verifying a check you
    just authored requires the same ``(space, snapshot)`` you wrote it to.

    The returned ``meta.auditTrail`` is COMPLETE — every entry, oldest first, with no
    cap or truncation on this read. (A trail is bounded once at WRITE time, and that
    elision always leaves a visible ``action="compacted"`` entry naming how many were
    dropped.) To audit provenance across MANY facts without pulling their bodies, use
    ``praxis_facts_by(..., fields="provenance")``.

    Returns a human summary plus a structured JSON block with the full candidate
    detail (``id``/``title``/``content``/``state``/``source``/``scope``/
    ``category``/``meta``/``auditTrail``...).
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/candidates/{cid}",
            headers=_headers(space, snapshot),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"Unknown fact {cid} — list ids with praxis_list_graph."
        return _friendly(exc)
    fact = resp.json()
    return _structured(
        f"fact {fact.get('id')} ({fact.get('state', '')})",
        fact,
    )


@mcp.tool()
def praxis_add_insight(
    insight: str,
    scope: str | None = None,
    category: str | None = None,
    source: str | None = None,
    meta: dict | None = None,
    on_conflict: str = "auto_resolve",
    derived_from: list[str] | None = None,
    raw: bool = False,
    space: str | None = None,
    snapshot: str | None = None,
) -> str:
    """Store a durable insight in the user's knowledge graph.

    By default the fact lands in your working memory. Pass BOTH ``space`` and ``snapshot``
    to write it into an ORG-SHARED snapshot instead — the factory seam: author a validation
    check into ``(space=<project>, snapshot="building-validation")``, a planning lens into
    ``planning-validation``, or a requirement into ``prd-<project>``. That is where the
    af-build / af-intake hooks READ, so a check written to working memory (no space/snapshot)
    is invisible to the factory. The server refuses a fact whose category/scope does not fit
    the destination snapshot's section (e.g. a non-check into ``building-validation``).

    Before calling, push the user to state a single specific, self-contained
    insight (one that stands on its own without surrounding chat context), and
    confirm the *exact* wording with them first — that confirmation is the human
    approval gate. The insight is stored fully approved (full credibility).

    ``scope``/``category``/``source`` and the free-form ``meta`` object are
    persisted onto the stored fact and returned on later reads (``scope``/
    ``category``/``source`` on ``praxis_get_context`` hits, ``meta`` on the
    candidate detail) — a writer-set value always wins over an ingestion-derived
    default. Use ``category`` to tag a fact's kind (e.g. ``"requirement"``) and
    ``meta`` for structured provenance (e.g. ``{"requirement_id": "R4"}``).

    ``on_conflict`` controls what happens when the insight contradicts an existing
    fact: ``"auto_resolve"`` (default) overwrites the conflicting fact (newest wins,
    loser rejected); ``"surface"`` keeps BOTH facts and raises a *pending*
    contradiction for human review (see ``praxis_get_contradictions`` /
    ``praxis_resolve_contradiction``) instead of silently deciding. Use ``"surface"``
    when a human should adjudicate conflicts rather than the newest write winning.

    **Do NOT pass ``on_conflict`` on a requirement-TICKET write** — a write carrying
    ``category="requirement"`` with ``meta.build_state`` (and ``meta.requirement_id``).
    ``on_conflict="surface"`` selects the additive-merge write policy, whose Augmenter
    can silently fold your brand-new ticket into a DIFFERENT, merely topically-similar
    existing ticket: the call returns ``action:"merged"`` with an ``id`` you never wrote
    and ``contradictionsSurfaced: 0``, your ticket is never created, and the existing
    ticket's ``content`` is overwritten — a corrupted plan snapshot that looks like a
    success. On a ticket write the ``on_conflict`` argument should simply NOT APPEAR in the
    call — not ``"surface"``, and not an explicit ``"auto_resolve"`` either; the backend
    answers any supplied ``onConflict`` on this path with a loud ``note`` telling you it was
    ignored and why. (``raw=True`` is likewise fine, and equally carries no Augmenter — which
    is why only ``"surface"`` corrupts.) The ticket path is identity-keyed on
    ``meta.requirement_id``: it upserts by that id — a re-file UPDATES the existing ticket in
    place — and never dedups, merges, or rejects, so ``on_conflict`` does not tune anything
    there; supplying it only risks diverting the write off that path and destroying data.
    Send ``category="requirement"`` on a ticket write as well: the backend now also
    recognises a ticket by its identity meta (``requirement_id`` + ``build_state``) when the
    label is missing or wrong, and stamps the category back on, but that is a repair, not
    the contract — label your writes.

    The response's ``factsRejected`` lists the ids of facts this write actually took down
    (``contradictionsSurfaced`` counts only *pending* contradictions and reads ``0`` even
    when facts went dark). A NON-EMPTY ``factsRejected`` means your write invalidated other
    knowledge — inspect those ids before moving on.

    ``derived_from`` records derivation provenance (gap H5): pass the ids of the
    facts this insight was derived from and the backend links a ``derived_from``
    edge (this fact -> each source) so an invalidated source can later surface
    this fact as suspect.

    ``raw=True`` is the fast lane for a trusted insert: the backend skips dedup and
    the LLM conflict/claim steps (so ``on_conflict`` no longer applies) while still
    scrubbing secrets via redaction. Use it for bulk trusted writes that time out on
    the per-item LLM conflict check; leave it ``False`` for normal reconciled writes.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if on_conflict not in ("auto_resolve", "surface"):
        return "on_conflict must be 'auto_resolve' or 'surface'."
    body: dict[str, object] = {"insight": insight, "onConflict": on_conflict, "raw": raw}
    if scope is not None:
        body["scope"] = scope
    if category is not None:
        body["category"] = category
    if source is not None:
        body["source"] = source
    if meta is not None:
        # Tag-normalize the applicability lanes (applies_to on a check, tags on a ticket) so an
        # authored check actually pins onto the ticket its tags name — see _normalize_applicability.
        body["meta"] = _normalize_applicability(meta)
    if derived_from:
        body["derivedFrom"] = derived_from
    try:
        resp = httpx.post(
            f"{identity.api_base()}/insights",
            json=body,
            headers=_headers(space, snapshot),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.TimeoutException:
        return _timeout_note("add_insight")
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    summary = payload.get("summary", "") or "insight stored"
    surfaced = payload.get("contradictionsSurfaced") or 0
    # factsRejected is the honest casualty list: contradictionsSurfaced can read 0 while the
    # write took other facts down, so surface both — and the backend's `note` (e.g. "onConflict
    # ignored on the identity-keyed ticket path") must reach the agent, not be swallowed.
    rejected = payload.get("factsRejected") or []
    note = payload.get("note") or ""
    if surfaced:
        summary = (
            f"{summary} — {surfaced} pending contradiction(s) raised; "
            "review with praxis_get_contradictions"
        )
    if rejected:
        summary = f"{summary} — {len(rejected)} fact(s) rejected by this write"
    if note:
        summary = f"{summary} — note: {note}"
    return _structured(
        summary,
        {
            "summary": payload.get("summary", ""),
            "action": payload.get("action"),
            "id": payload.get("id"),
            "onConflict": payload.get("onConflict"),
            "contradictionsSurfaced": surfaced,
            "factsRejected": rejected,
            "note": note,
        },
    )


@mcp.tool()
def praxis_add_insights(
    insights: list[dict],
    on_conflict: str = "auto_resolve",
    raw: bool = False,
    space: str | None = None,
    snapshot: str | None = None,
) -> str:
    """Store many already-distilled insights in ONE call (bulk sibling of praxis_add_insight).

    Use this when you have several confirmed, self-contained insights to persist
    at once (e.g. the learnings from a whole session) instead of calling
    ``praxis_add_insight`` repeatedly — it's one round-trip and the backend writes
    them serially, which is both faster and gentler on the write path than firing
    many concurrent single-insight calls.

    By default the whole batch lands in your working memory. Pass BOTH ``space`` and
    ``snapshot`` to write it into an ORG-SHARED snapshot instead — the same factory seam
    as ``praxis_add_insight`` (e.g. a whole plan into ``(space=<project>,
    snapshot="prd-<project>")``, which is where the af-build / af-intake hooks READ).
    Passing exactly one of the pair is a misconfiguration and raises. Checks
    (``category="check"``) are the one exception: they are identity-keyed and must be
    authored one at a time via ``praxis_add_insight``, so the backend rejects a batched check.

    ``insights`` is a list of objects, each shaped like a ``praxis_add_insight``
    call: ``{"insight": str, "scope"?: str, "category"?: str, "source"?: str,
    "meta"?: object}``. As with the single tool, confirm the exact wording of each
    insight with the user first — that confirmation is the human approval gate.

    ``on_conflict`` is batch-level and mirrors ``praxis_add_insight``:
    ``"auto_resolve"`` (default) overwrites a conflicting fact; ``"surface"`` keeps
    both and raises a pending contradiction for human review.

    ``raw=True`` is the fast lane for a trusted bulk insert: the backend skips dedup
    and the LLM conflict/claim steps (so ``on_conflict`` no longer applies) while
    still scrubbing secrets via redaction. Use it for large trusted batches (e.g. 71
    items) that time out on the per-item LLM conflict check; leave it ``False`` for
    normal reconciled writes.

    The same ticket-write rule as ``praxis_add_insight`` applies here and bites harder,
    because ``on_conflict`` is batch-level: if any item is a requirement TICKET
    (``category="requirement"`` with ``meta.build_state``/``meta.requirement_id``), do not
    pass ``on_conflict`` at all — ``"surface"`` selects the additive-merge policy whose
    Augmenter can fold a brand-new ticket into a different existing one, returning
    ``action:"merged"`` with an id you never wrote while your ticket is never created.

    Returns a structured JSON block with one result per insight (in order), each
    carrying ``ok``/``id``/``action``/``retrievable`` (read-your-writes confirmed),
    ``contradictionsSurfaced``/``factsRejected`` (the ids that item's write actually took
    down — a non-empty list means other knowledge went dark, which the surfaced COUNT does
    not tell you), and, on a per-item failure, an ``error`` — a bad item never aborts the rest.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if on_conflict not in ("auto_resolve", "surface"):
        return "on_conflict must be 'auto_resolve' or 'surface'."
    if not isinstance(insights, list) or not insights:
        return "insights must be a non-empty list of insight objects."
    # Tag-normalize each item's applicability lanes (applies_to / tags), same as praxis_add_insight.
    insights = [
        {**it, "meta": _normalize_applicability(it["meta"])} if isinstance(it, dict) and "meta" in it
        else it
        for it in insights
    ]
    body = {"insights": insights, "onConflict": on_conflict, "raw": raw}
    try:
        resp = httpx.post(
            f"{identity.api_base()}/insights/batch",
            json=body,
            headers=_headers(space, snapshot),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.TimeoutException:
        return _timeout_note("add_insights")
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    results = payload.get("results", [])
    ok = sum(1 for r in results if r.get("ok"))
    surfaced = sum(r.get("contradictionsSurfaced") or 0 for r in results)
    # Same reasoning as praxis_add_insight: the surfaced COUNT reads 0 while facts go dark,
    # so roll up factsRejected across the batch and say it out loud in the summary line.
    rejected = sum(len(r.get("factsRejected") or []) for r in results)
    summary = f"stored {ok}/{payload.get('count', len(results))} insight(s)"
    if surfaced:
        summary += (
            f" — {surfaced} pending contradiction(s) raised; "
            "review with praxis_get_contradictions"
        )
    if rejected:
        summary += f" — {rejected} fact(s) rejected by this batch (see factsRejected per result)"
    return _structured(summary, {"count": payload.get("count"), "results": results})


@mcp.tool()
def praxis_ingest(
    text: str,
    source: str | None = None,
    state: str = "active",
    on_conflict: str = "auto_resolve",
    derived_from: list[str] | None = None,
) -> str:
    """Ingest a raw document through Praxis's distillation pipeline.

    Unlike ``praxis_add_insight`` (one already-distilled fact), this hands a raw
    document (a note, a transcript, a file's contents) to the backend, which
    distills it into atomic facts, dedupes, and reconciles conflicts. ``state``
    is "active" (live knowledge) or "proposed" (staged for review).

    ``on_conflict`` mirrors ``praxis_add_insight``: ``"auto_resolve"`` (default)
    rejects the losing side of a detected clash; ``"surface"`` keeps both facts and
    raises a *pending* contradiction for human review. Returns a structured JSON
    block with per-document results (``id``/``action``/``surfaced``).

    ``derived_from`` records derivation provenance (gap H5): the ids of the facts
    this document was derived from; the backend links a ``derived_from`` edge from
    each distilled fact to those sources.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if on_conflict not in ("auto_resolve", "surface"):
        return "on_conflict must be 'auto_resolve' or 'surface'."
    body: dict[str, object] = {
        "documents": [{"text": text, "source": source}],
        "state": state,
        "onConflict": on_conflict,
    }
    if derived_from:
        body["derivedFrom"] = derived_from
    try:
        resp = httpx.post(
            f"{identity.api_base()}/ingest",
            json=body,
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.TimeoutException:
        return _timeout_note("ingest")
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    return _structured(
        f"ingested {payload.get('count', 0)} document(s)",
        payload,
    )


@mcp.tool()
def praxis_ingest_session(narrative: str, source: str | None = None) -> str:
    """Distill a solved-problem coding session into PROPOSED knowledge candidates.

    Hand the rendered narrative of a session you just finished (the problem, what was
    tried and failed, the fix, why it works, how to prevent recurrence) to Praxis. The
    backend runs the session distiller and writes each durable insight as a
    ``proposed`` candidate — staged for human review, NOT added active. This is the
    ``/ce-compound``-style capture path; use ``praxis_add_insight`` instead for a
    single, already-distilled fact you want stored at full confidence.

    ``source`` is optional and, when given, must look like ``session/<id>``; omit it
    and the backend generates one. Returns a human summary plus a JSON block with the
    created candidates (``id``/``scope``/``category``).
    """
    if (hint := _not_ready()) is not None:
        return hint
    body: dict[str, object] = {"narrative": narrative}
    if source is not None:
        body["source"] = source
    try:
        resp = httpx.post(
            f"{identity.api_base()}/ingest/session",
            json=body,
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.TimeoutException:
        return _timeout_note("ingest_session")
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    return _structured(
        f"distilled {payload.get('count', 0)} proposed candidate(s) "
        f"from session {payload.get('source', '')}",
        payload,
    )


@mcp.tool()
def praxis_record_outcome(fact_id: str, outcome: str,
                          space: str | None = None, snapshot: str | None = None) -> str:
    """Feed a downstream verification result back into a fact's trust (gap H1).

    Pass BOTH ``space`` and ``snapshot`` to record the outcome on a fact that lives in an
    org-shared snapshot — the factory records ticket outcomes on ``prd-<project>`` (its
    canonical project graph), so a regress of a snapshot ticket needs that ``(space,
    snapshot)``; omit both for a working-memory fact.

    Records whether acting on a fact actually worked. ``outcome`` is
    ``"succeeded"`` / ``"failed"`` (``"success"``/``"failure"``/``"true"``/
    ``"false"`` and a bare bool are also accepted). A success increments the fact's
    success count and a failure its failure count — retrieval folds these into a
    utility weighting so a repeatedly-failed fact sinks in ranking and a proven one
    holds. Find the fact id via ``praxis_get_context`` / ``praxis_list_graph``.
    """
    if (hint := _not_ready()) is not None:
        return hint
    token = str(outcome).strip().lower()
    if token in ("succeeded", "success", "succeed", "true", "ok", "pass", "passed"):
        success = True
    elif token in ("failed", "failure", "fail", "false", "error", "no"):
        success = False
    else:
        return "outcome must be 'succeeded' or 'failed'."
    try:
        resp = httpx.post(
            f"{identity.api_base()}/facts/{fact_id}/outcome",
            json={"success": success},
            headers=_headers(space, snapshot),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    return f"Recorded {'success' if success else 'failure'} on fact id={fact_id}."


@mcp.tool()
def praxis_record_episode(
    text: str,
    alternatives: list[str] | None = None,
    outcome: str = "pending",
    derived_from: list[str] | None = None,
    decided_at: str | None = None,
) -> str:
    """Record a decision-log episode — store-only, out of semantic recall (gap H4).

    An episode is a "why we decided X" note: it is stored whole and append-only,
    bypassing distillation/dedup/contradiction, and is excluded from
    ``praxis_get_context`` by default so rationale never pollutes semantic recall.
    Use this (rather than ``praxis_add_insight(category="episodic")``) for decision
    journals. ``alternatives`` are the options considered but not chosen;
    ``outcome`` tracks how the decision turned out (e.g. ``"pending"`` /
    ``"succeeded"`` / ``"failed"``); ``derived_from`` links the facts the decision
    was based on (H5); ``decided_at`` is an ISO timestamp (defaults to now).
    """
    if (hint := _not_ready()) is not None:
        return hint
    if not text.strip():
        return "Pass non-empty episode text."
    episode: dict[str, object] = {"outcome": outcome}
    if alternatives:
        episode["alternatives"] = alternatives
    if decided_at is not None:
        episode["decided_at"] = decided_at
    body: dict[str, object] = {
        "insight": text,
        "category": "episodic",
        "meta": {"episode": episode},
    }
    if derived_from:
        body["derivedFrom"] = derived_from
    try:
        resp = httpx.post(
            f"{identity.api_base()}/insights",
            json=body,
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.TimeoutException:
        return _timeout_note("record_episode")
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    return _structured(
        payload.get("summary", "") or "recorded episode",
        {"summary": payload.get("summary", ""), "action": payload.get("action"), "id": payload.get("id")},
    )


def _fmt_side(label: str, side: dict) -> str:
    state = side.get("state", "")
    sid = side.get("id", "")
    content = side.get("content") or side.get("title") or ""
    return f"  {label} [id={sid} | {state}]: {content}"


@mcp.tool()
def praxis_get_contradictions(space: str | None = None, snapshot: str | None = None) -> str:
    """List the flagged contradictions in the user's knowledge graph.

    Each entry is a pair of facts the conflict detector judged to contradict each
    other; both are kept in the graph until resolved. Use this to review what is
    flagged and why, then call ``praxis_resolve_contradiction`` to settle a pair.

    Pass BOTH ``space`` and ``snapshot`` to review contradictions raised INSIDE an org-shared
    snapshot (e.g. an ``on_conflict="surface"`` clash from authoring a check into
    ``building-validation``); omit both for working memory. Use the same ``(space, snapshot)``
    you wrote to — a contradiction lives in the graph the conflicting facts live in.
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/contradictions",
            headers=_headers(space, snapshot),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    clusters = resp.json()
    if not clusters:
        return "No contradictions are currently flagged."
    lines = [f"{len(clusters)} contradiction(s) flagged:"]
    for c in clusters:
        slot = c.get("slot") or {}
        slot_label = (
            f" on {slot.get('subject')}/{slot.get('attribute')}"
            if slot.get("subject")
            else ""
        )
        members = c.get("members") or []
        lines.append(
            f"\n[{c.get('id')}]  ({c.get('status', 'pending')}){slot_label}"
            f" — {len(members)} competing fact(s)"
        )
        for i, m in enumerate(members):
            lines.append(_fmt_side(chr(ord("A") + i), m))
        for p in c.get("pairs") or []:
            lines.append(f"    resolve pair id: {p.get('id')}")
    return "\n".join(lines)


@mcp.tool()
def praxis_resolve_contradiction(
    pair_id: str,
    keep: str | None = None,
    custom_text: str | None = None,
    space: str | None = None,
    snapshot: str | None = None,
) -> str:
    """Resolve a flagged contradiction cluster (from ``praxis_get_contradictions``).

    Pass BOTH ``space`` and ``snapshot`` (the same ones the contradiction was listed under) to
    settle a clash raised inside an org-shared snapshot; omit both for working memory.

    A cluster is settled by saying which members to ``keep``:
    - ``"all"`` — every member genuinely holds (a *false positive*, e.g. the facts
      describe different actors/scopes). Keep them all active; nothing is lost.
    - ``"none"`` — reject every member.
    - one or more fact ids (space- or comma-separated, e.g. ``"f12 f34"``) — keep
      those active and reject the rest. A single id keeps one side (the classic
      pick-a-winner).

    Or pass ``custom_text`` instead to replace the whole cluster with one reconciled
    fact. Confirm the choice with the user before calling; resolution mutates the
    graph.
    """
    if (hint := _not_ready()) is not None:
        return hint
    has_custom = bool(custom_text and custom_text.strip())
    has_keep = bool(keep and keep.strip())
    if not has_custom and not has_keep:
        return (
            "Pass keep ('all', 'none', or fact ids to keep) or custom_text "
            "(a reconciled fact)."
        )
    body: dict[str, object] = {}
    if has_custom:
        body["customText"] = custom_text
    else:
        normalized = keep.strip().lower()
        if normalized in ("all", "none"):
            body["keep"] = normalized
        else:
            body["keep"] = [tok for tok in re.split(r"[,\s]+", keep.strip()) if tok]
    try:
        resp = httpx.post(
            f"{identity.api_base()}/contradictions/{pair_id}/resolve",
            json=body,
            headers=_headers(space, snapshot),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    return f"Resolved contradiction {pair_id}: {resp.json()}"


@mcp.tool()
def praxis_list_graph(state: str | None = None) -> str:
    """List every fact in the user's knowledge graph (not similarity-ranked).

    Unlike ``praxis_get_context`` (top-k by relevance), this returns the full
    graph. Pass ``state`` to filter (e.g. "active", "proposed", "decayed");
    omit it for all states. Use this to audit what is stored, find ids to edit
    or resolve, or review the whole graph.
    """
    if (hint := _not_ready()) is not None:
        return hint
    params = {"state": state} if state else {}
    try:
        resp = httpx.get(
            f"{identity.api_base()}/candidates",
            params=params,
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    facts = resp.json()
    if not facts:
        return "The knowledge graph is empty (for this filter)." if state else "The knowledge graph is empty."
    lines = [f"{len(facts)} fact(s){f' in state {state!r}' if state else ''}:"]
    for f in facts:
        content = str(f.get("content") or f.get("title") or "")
        if len(content) > 160:
            content = content[:157] + "…"
        lines.append(f"  [id={f.get('id')} | {f.get('state', '')}] {content}")
    return "\n".join(lines)


@mcp.tool()
def praxis_insert_fact(
    title: str,
    content: str,
    provenance: str | None = None,
    category: str | None = None,
    meta: dict | None = None,
    derived_from: list[str] | None = None,
    space: str | None = None,
    snapshot: str | None = None,
) -> str:
    """Insert a fact directly into the graph, bypassing the ingestion pipeline.

    Pass BOTH ``space`` and ``snapshot`` to insert into an org-shared snapshot (a check into
    ``building-validation``, a ticket into ``prd-<project>``) instead of working memory.

    This is a *raw* write — no redaction, dedup, or conflict handling — and the
    fact lands in the "proposed" state for review. For normal human-approved
    knowledge that should reconcile with existing facts, use ``praxis_add_insight``
    (which runs the full ingestion pipeline and lands active) instead.

    ``category`` tags the fact's kind (e.g. ``"requirement"``/``"learning"``);
    ``meta`` is a free-form object persisted onto the fact (structured provenance).
    Two keys in it are not stored verbatim: ``title`` mirrors the required ``title``
    argument, and a supplied ``auditTrail`` is MERGED — its entries are kept in order
    and the backend appends its own ``"created"`` entry after them. That is what makes
    a snapshot-to-snapshot move carry a ticket's history across intact while still
    recording that the move happened; read it back with ``praxis_get_fact`` or
    ``praxis_facts_by(..., fields="provenance")``. A supplied trail that is not a list
    of objects is not provenance and is dropped.
    ``derived_from`` is the ids of the facts this one was derived from — the backend
    links a ``derived_from`` edge (this fact -> each source) so an invalidated source
    can later surface this fact as suspect (gap H5). These let a manual-repair insert
    carry the same structured data ``praxis_add_insight`` does.
    """
    if (hint := _not_ready()) is not None:
        return hint
    body: dict[str, object] = {"title": title, "content": content}
    if provenance is not None:
        body["provenance"] = provenance
    if category is not None:
        body["category"] = category
    if meta is not None:
        body["meta"] = meta
    if derived_from:
        body["derivedFrom"] = derived_from
    try:
        resp = httpx.post(
            f"{identity.api_base()}/candidates",
            json=body,
            headers=_headers(space, snapshot),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    c = resp.json()
    return f"Inserted fact id={c.get('id')} (state={c.get('state')})."


@mcp.tool()
def praxis_edit_fact(
    cid: str,
    title: str | None = None,
    content: str | None = None,
    provenance: str | None = None,
    category: str | None = None,
    meta: dict | None = None,
    derived_from: list[str] | None = None,
    on_conflict: str = "none",
    space: str | None = None,
    snapshot: str | None = None,
) -> str:
    """Edit an existing fact in place (find its id via ``praxis_list_graph``).

    Pass only the fields to change — ``title``, ``content``, ``provenance``,
    ``category``, ``meta`` (merged into the fact's existing meta), and/or
    ``derived_from`` (ids to attach as ``derived_from`` edges from this fact).
    Confirm edits with the user first; this mutates stored knowledge.

    Pass BOTH ``space`` and ``snapshot`` to edit a fact that lives in an org-shared snapshot
    (e.g. an idempotent update of a check in ``building-validation``, or a ticket-state edit
    in ``prd-<project>``); omit both for a working-memory fact.

    That snapshot support has ONE gate: a ``prd-<project>`` plan snapshot that has been
    BLESSED is frozen, and an edit against it is refused by the bless-state guard with a
    400 whose ``detail`` reads like "plan 'prd-<project>' is blessed — re-arm the planning
    marker (stamp_planning) to mutate this snapshot". Editing a fact in a blessed plan
    snapshot therefore requires arming the planning marker FIRST —
    ``praxis_planning_marker(project, owner=..., space=..., snapshot=...)``, then re-issue
    the edit, then re-bless with ``clear=True``. The same gate applies
    to ``praxis_delete_fact`` against a blessed plan snapshot.

    ``on_conflict`` defaults to ``"none"``: an edit is a **literal write** — only
    this fact's own fields change and no other fact is touched. Editing a field is
    not an assertion of new knowledge to reconcile, so it must never silently reject
    a different fact. Opt in only when you want the edited content reconciled like an
    ``add_insight``: ``"surface"`` keeps every fact and raises a *pending*
    contradiction for each clash (review via ``praxis_get_contradictions``);
    ``"auto_resolve"`` supersedes each clashing fact (the edit wins, the loser is
    rejected). Reconciliation runs only when ``content`` actually changes.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if on_conflict not in ("none", "surface", "auto_resolve"):
        return "on_conflict must be 'none', 'surface', or 'auto_resolve'."
    body: dict[str, object] = {}
    if title is not None:
        body["title"] = title
    if content is not None:
        body["content"] = content
    if provenance is not None:
        body["provenance"] = provenance
    if category is not None:
        body["category"] = category
    if meta is not None:
        body["meta"] = meta
    if derived_from:
        body["derivedFrom"] = derived_from
    if not body:
        return (
            "Nothing to edit — pass title, content, provenance, "
            "category, meta, and/or derived_from."
        )
    # Send onConflict only when opting into reconciliation, so a plain edit stays a
    # minimal literal-write body (the backend also defaults absent -> "none").
    if on_conflict != "none":
        body["onConflict"] = on_conflict
    try:
        resp = httpx.patch(
            f"{identity.api_base()}/candidates/{cid}",
            json=body,
            headers=_headers(space, snapshot),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    c = resp.json()
    return f"Edited fact id={c.get('id')} (state={c.get('state')})."


@mcp.tool()
def praxis_record_derivation(fact_id: str, source_ids: list[str]) -> str:
    """Attach a ``derived_from`` edge from a fact to each of its sources (gap H5).

    Links ``fact_id`` to the facts it was derived from, so an invalidated source
    later surfaces this fact as suspect (see ``praxis_get_stale_derivations`` /
    ``praxis_dependents``). This is the direct way to create or repair a derivation
    edge between two *existing* facts — use it to relink an edge a merge destroyed,
    or to connect a fact written via ``praxis_insert_fact`` to its sources. Both the
    fact and every source must already exist (find ids via ``praxis_list_graph``).
    Idempotent; self-edges are skipped.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if not fact_id or not source_ids:
        return "Pass a fact_id and a non-empty list of source_ids."
    body: dict[str, object] = {"factId": fact_id, "sourceIds": source_ids}
    try:
        resp = httpx.post(
            f"{identity.api_base()}/derivations",
            json=body,
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    d = resp.json()
    srcs = ", ".join(d.get("sourceIds", []))
    return f"Recorded derived_from edge(s): {d.get('factId')} -> [{srcs}]."


@mcp.tool()
def praxis_promote_fact(
    cid: str,
    target_state: str | None = None,
    space: str | None = None,
    snapshot: str | None = None,
) -> str:
    """Promote a fact through its lifecycle (the dashboard "promote" action).

    Moves a fact forward one step (e.g. ``proposed`` -> ``active``); pass
    ``target_state`` to force a specific destination, or omit it to let the
    backend advance to the next state. Find the id via ``praxis_list_graph``.
    Confirm with the user first — this changes what retrieval reads.

    Omit BOTH ``space`` and ``snapshot`` to address a candidate in your working memory
    (the default). Pass BOTH to address a fact that lives inside an ORG-SHARED snapshot —
    the repair seam for plan state, e.g. un-rejecting (``target_state="active"``) a
    requirement ticket in ``(space=<project>, snapshot="prd-<project>")`` that a bad
    merge wrongly rejected. Without the pair the request resolves against working memory
    and 404s on any snapshot-resident fact; passing exactly one of the pair raises.

    This is also the un-reject: a rejected row is still there (that is the whole problem
    with reject — see ``praxis_reject_fact``) and promoting it back to ``active`` revives
    it. A DELETED fact is gone and there is nothing to promote, which is the point: use
    ``praxis_delete_fact`` when a fact should not exist, and this tool when a fact that
    should exist was wrongly hidden.
    """
    if (hint := _not_ready()) is not None:
        return hint
    body: dict[str, object] = {}
    if target_state is not None:
        body["targetState"] = target_state
    try:
        resp = httpx.post(
            f"{identity.api_base()}/candidates/{cid}/promote",
            json=body,
            headers=_headers(space, snapshot),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    c = resp.json()
    return f"Promoted fact id={c.get('id')} (state={c.get('state')})."


@mcp.tool()
def praxis_reject_fact(
    cid: str,
    reason: str | None = None,
    space: str | None = None,
    snapshot: str | None = None,
) -> str:
    """SOFT-HIDE a fact, keeping the row (narrow/specialist — **to remove something, use
    ``praxis_delete_fact``**).

    Reject does NOT remove anything. The row stays in the graph in state ``rejected``:
    invisible to active queries, but still fetchable, still counted by readers that span
    all states, and — for a requirement ticket — STILL OCCUPYING its
    ``meta.requirement_id``. That last part is the trap: rejecting a ticket instead of
    deleting it is exactly how a plan snapshot ends up with a stranded twin, two facts
    claiming one ``requirement_id``, one of them a ghost nobody can see but every identity
    lookup trips over. If the thing should not exist, delete it.

    Use reject only when you specifically want the row PRESERVED: an audit trail of what
    was asserted and disowned, or the stale-dependent review propagation (a rejection
    flags every learning derived from this fact as suspect — see
    ``praxis_get_stale_derivations``). Pass an optional ``reason`` for that audit trail.
    Find the id via ``praxis_list_graph``. Confirm with the user first. A rejection is
    reversible via ``praxis_promote_fact``.

    Omit BOTH ``space`` and ``snapshot`` to address a candidate in your working memory
    (the default). Pass BOTH to address a fact inside an ORG-SHARED snapshot, e.g. a
    requirement ticket in ``(space=<project>, snapshot="prd-<project>")``. Without the
    pair the request resolves against working memory and 404s on any snapshot-resident
    fact; passing exactly one of the pair raises.
    """
    if (hint := _not_ready()) is not None:
        return hint
    body: dict[str, object] = {}
    if reason is not None:
        body["reason"] = reason
    try:
        resp = httpx.post(
            f"{identity.api_base()}/candidates/{cid}/reject",
            json=body,
            headers=_headers(space, snapshot),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    c = resp.json()
    return f"Rejected fact id={c.get('id')} (state={c.get('state')})."


@mcp.tool()
def praxis_delete_fact(
    cid: str | None = None,
    space: str | None = None,
    snapshot: str | None = None,
    requirement_id: str | None = None,
) -> str:
    """REMOVE a fact from the graph outright — the removal verb, for anything that should
    not exist.

    Deletes the fact in ANY state; no reject step is needed first, and its edges and claims
    cascade away with it. This is what you want for a duplicate ticket, a mis-filed ticket,
    a node a bad merge corrupted, or any fact that is simply wrong to have. It is
    irreversible — confirm with the user first — but it genuinely removes the row and frees
    its ``meta.requirement_id``, which ``praxis_reject_fact`` does NOT do (a rejected ticket
    still holds its id and becomes a stranded twin). Reach for reject only in the narrow
    case where you need the row preserved for audit or want the stale-dependent review
    propagation.

    Address the fact EITHER by ``cid`` (from ``praxis_list_graph`` / ``praxis_facts_by``)
    OR by ``requirement_id`` — the identity a ticket actually carries in
    ``meta.requirement_id`` (e.g. ``"R7"``), which saves fumbling an opaque 32-hex id.
    Identity lookup requires BOTH ``space`` and ``snapshot`` (that pair is the search
    scope), spans ALL states, and does not pin ``category`` — a corrupted ticket may have
    lost its ``category="requirement"`` label and must still be findable. If more than one
    fact carries that ``requirement_id`` this REFUSES and names every match with its state
    rather than guessing: two facts sharing one ``requirement_id`` is the corruption
    signature itself, and which one dies is a decision for you, not for a tiebreak rule.
    Delete the twin you identified by its ``cid``.

    Omit BOTH ``space`` and ``snapshot`` to address a candidate in your working memory
    (the default). Pass BOTH to address a fact inside an ORG-SHARED snapshot, e.g. a
    duplicate requirement ticket in ``(space=<project>, snapshot="prd-<project>")``.
    Without the pair the request resolves against working memory and 404s on any
    snapshot-resident fact; passing exactly one of the pair raises.

    A delete against a BLESSED ``prd-<project>`` plan snapshot is refused by the
    bless-state guard with a 400 — same rule as ``praxis_edit_fact``: re-arm the planning
    marker first with ``praxis_planning_marker(project, owner=..., space=..., snapshot=...)``,
    and re-bless with ``clear=True`` afterwards. The 400's ``detail`` names the guard.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if bool(cid) == bool(requirement_id):
        return (
            "Pass exactly one of cid or requirement_id "
            "(cid for a known fact id, requirement_id to address a ticket by its identity)."
        )
    if requirement_id:
        cid, err = _resolve_requirement_cid(requirement_id, space, snapshot)
        if err is not None:
            return err
    try:
        resp = httpx.delete(
            f"{identity.api_base()}/candidates/{cid}",
            headers=_headers(space, snapshot),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    return f"Deleted fact id={cid}."


@mcp.tool()
def praxis_planning_marker(
    project: str,
    owner: str | None = None,
    clear: bool = False,
    space: str | None = None,
    snapshot: str | None = None,
) -> str:
    """ARM or DISARM a project's planning marker — the gate that allows edits to a blessed plan.

    A ``prd-<project>`` plan snapshot is WRITE-PROTECTED once blessed: ``praxis_edit_fact`` and
    ``praxis_delete_fact`` against it are refused with a 400 reading "plan '<snapshot>' is blessed
    — re-arm the planning marker (stamp_planning) to mutate this snapshot". This tool is how you
    follow that instruction. Without it the refusal is a dead end — arming used to live only in a
    Python hook helper, so an agent working through these tools could not comply with the very
    message it was handed, and reasonably concluded snapshot facts were simply not editable.

    Read the marker's states carefully; they are easy to invert:

    * ``planning_owner`` SET (and fresh) -> ARMED: the plan is open for editing.
    * ``planning_owner`` NULL with ``blessed_at`` set -> BLESSED: edits are REFUSED. This is the
      state a finished plan rests in, so "the marker was cleared" means blessed, NOT unprotected.

    Pass ``owner`` (any stable session identifier) to ARM before a repair, then call again with
    ``clear=True`` to re-bless when you are done — leaving a plan armed leaves it unprotected, and
    the marker also goes stale on its own after an hour. Pass BOTH ``space`` and ``snapshot`` to
    address the project's plan snapshot (``space=<project>``, ``snapshot="prd-<project>"``), which
    is where the guard reads. With neither argument this only ensures the marker fact EXISTS
    (the greenfield bootstrap) and changes no state.

    Editing a ticket is usually better done with ``praxis_add_insight`` carrying the same
    ``meta.requirement_id`` — that path is identity-keyed, updates in place, and is NOT
    bless-guarded. Reach for arming when you need a meta-only edit, or to amend a fact that has no
    identity key of its own.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if owner and clear:
        return "Pass either owner (to arm) or clear=True (to disarm), not both."
    body: dict[str, object] = {"project": project}
    if owner:
        body["owner"] = owner
    if clear:
        body["clear"] = True
    try:
        resp = httpx.post(
            f"{identity.api_base()}/planning-marker",
            json=body,
            headers=_headers(space, snapshot),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    m = resp.json()
    state = {True: "ARMED (plan is editable)", False: "BLESSED (edits refused)"}.get(
        m.get("armed"), "unchanged (marker ensured only)"
    )
    return _structured(f"planning marker for {project!r}: {state}", m)


@mcp.tool()
def praxis_clear_graph() -> str:
    """Truncate the caller's entire live graph (the dashboard "clear graph" action).

    Deletes every fact and edge you own in the active org; other members' rows
    are untouched. This is destructive — consider ``praxis_save_snapshot`` first
    so you can restore. Confirm with the user before calling.
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.post(
            f"{identity.api_base()}/graph/clear",
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    return f"Cleared {resp.json().get('cleared', 0)} fact(s) from the live graph."


@mcp.tool()
def praxis_list_snapshots(space: str | None = None) -> str:
    """List the saved snapshots inside a space (the dashboard Snapshots panel).

    A snapshot is an org-shared saved graph state stored in a ``space`` (a project
    folder any org member can read); restore one into your working memory via
    ``praxis_load_snapshot``. ``space`` defaults to the one selected with
    ``praxis_select_space``. Returns each snapshot's name, node count, and creation
    time.
    """
    if (hint := _not_ready()) is not None:
        return hint
    space = _resolve_space(space)
    if not space:
        return "Pass a space (or set a default with praxis_select_space)."
    try:
        resp = httpx.get(
            f"{identity.api_base()}/snapshots",
            params={"space": space},
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    snaps = resp.json().get("snapshots", [])
    if not snaps:
        return f"No snapshots in space {space!r}."
    lines = [f"{len(snaps)} snapshot(s) in space {space!r}:"]
    for s in snaps:
        lines.append(
            f"  {s.get('snapshot')} — {s.get('count')} node(s)"
            f"{f' (saved {s.get('createdAt')})' if s.get('createdAt') else ''}"
        )
    return "\n".join(lines)


@mcp.tool()
def praxis_save_snapshot(snapshot: str, space: str | None = None) -> str:
    """Dump your working memory into a snapshot in a space (the "save snapshot").

    Copies your current working memory (your private live graph) into the org-shared
    snapshot ``snapshot`` inside ``space``, creating or overwriting it, so any org
    member can later restore it with ``praxis_load_snapshot``. ``space`` defaults to
    the one selected with ``praxis_select_space``.
    """
    if (hint := _not_ready()) is not None:
        return hint
    space = _resolve_space(space)
    if not space:
        return "Pass a space (or set a default with praxis_select_space)."
    if not snapshot.strip():
        return "Pass a non-empty snapshot name."
    try:
        resp = httpx.post(
            f"{identity.api_base()}/snapshots",
            json={"space": space, "snapshot": snapshot.strip()},
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    s = resp.json()
    return (
        f"Saved snapshot {s.get('snapshot')!r} in space {s.get('space', space)!r} "
        f"with {s.get('count', 0)} node(s)."
    )


@mcp.tool()
def praxis_load_snapshot(snapshot: str, space: str | None = None, mode: str = "replace") -> str:
    """Load a space's snapshot into your working memory (the "load snapshot").

    Copies the org-shared snapshot ``snapshot`` from ``space`` into your private
    working memory. ``mode="replace"`` (default) truncates your working memory then
    loads the snapshot; ``mode="add"`` merges it in, replacing only nodes it shares
    by id. ``space`` defaults to the one selected with ``praxis_select_space``.
    Confirm with the user first — ``replace`` discards your current working memory
    (save it first with ``praxis_save_snapshot`` if unsure).
    """
    if (hint := _not_ready()) is not None:
        return hint
    space = _resolve_space(space)
    if not space:
        return "Pass a space (or set a default with praxis_select_space)."
    if not snapshot.strip():
        return "Pass a non-empty snapshot name."
    mode = mode.strip().lower()
    if mode not in ("add", "replace"):
        return "mode must be 'add' or 'replace'."
    try:
        resp = httpx.post(
            f"{identity.api_base()}/snapshots/load",
            json={"space": space, "snapshot": snapshot.strip(), "mode": mode},
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return (
                f"Unknown snapshot {snapshot!r} in space {space!r} — "
                "list them with praxis_list_snapshots."
            )
        return _friendly(exc)
    return (
        f"Loaded {resp.json().get('loaded', 0)} node(s) from snapshot "
        f"{snapshot.strip()!r} in space {space!r} ({mode})."
    )


@mcp.tool()
def praxis_delete_snapshot(snapshot: str, space: str | None = None) -> str:
    """Delete a snapshot from a space (the dashboard "delete snapshot" action).

    Removes the org-shared snapshot ``snapshot`` from ``space`` (also unmounting it
    for any viewers who mounted it); working memory is unaffected. ``space`` defaults
    to the one selected with ``praxis_select_space``. Confirm with the user first.
    """
    if (hint := _not_ready()) is not None:
        return hint
    space = _resolve_space(space)
    if not space:
        return "Pass a space (or set a default with praxis_select_space)."
    if not snapshot.strip():
        return "Pass a non-empty snapshot name."
    try:
        resp = httpx.request(
            "DELETE",
            f"{identity.api_base()}/snapshots",
            json={"space": space, "snapshot": snapshot.strip()},
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    return f"Deleted snapshot {resp.json().get('deleted', snapshot.strip())!r} from space {space!r}."


@mcp.tool()
def praxis_copy_snapshot_to_org(
    snapshot: str,
    target_org: str,
    target_space: str,
    space: str | None = None,
    target_snapshot: str | None = None,
) -> str:
    """Copy a snapshot into another org you belong to (cross-org share).

    Shares a snapshot between two orgs the SAME login is a member of: the snapshot
    ``snapshot`` is read from ``space`` in your active org and copied into
    ``target_space`` in ``target_org`` under ``target_snapshot`` (defaults to the
    same snapshot name). ``space`` defaults to the one selected with
    ``praxis_select_space``. Ids and embeddings are preserved, so the copy is
    identical — load it there with ``praxis_load_snapshot`` after
    ``praxis_select_org(target_org)``. The copy never overwrites: it fails if
    ``target_space`` in ``target_org`` already has a snapshot by that name (rename
    via ``target_snapshot``). See ``praxis_whoami`` for your orgs. To copy every
    snapshot of a space instead of one, use ``praxis_copy_space_to_org``.
    """
    if (hint := _not_ready()) is not None:
        return hint
    space = _resolve_space(space)
    if not space:
        return "Pass a space (or set a default with praxis_select_space)."
    if not snapshot.strip():
        return "Pass a non-empty snapshot name (see praxis_list_snapshots)."
    if not target_org.strip():
        return "Pass a target_org you belong to (see praxis_whoami)."
    if not target_space.strip():
        return "Pass a target_space (the space in target_org to copy into)."
    payload: dict[str, object] = {
        "space": space,
        "snapshot": snapshot.strip(),
        "targetOrg": target_org.strip(),
        "targetSpace": target_space.strip(),
    }
    if target_snapshot and target_snapshot.strip():
        payload["targetSnapshot"] = target_snapshot.strip()
    try:
        resp = httpx.post(
            f"{identity.api_base()}/snapshots/copy-to-org",
            json=payload,
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return (
                f"Unknown snapshot {snapshot!r} in space {space!r} — "
                "list them with praxis_list_snapshots."
            )
        if exc.response.status_code == 409:
            return (
                f"A snapshot by that name already exists in space {target_space!r} of "
                f"{target_org!r}. Pass a different target_snapshot (copies never overwrite)."
            )
        if exc.response.status_code == 403:
            return (
                f"You are not a member of org {target_org!r} — join it first "
                "(praxis_join_org) or pick another (praxis_whoami)."
            )
        return _friendly(exc)
    s = resp.json()
    return (
        f"Copied snapshot into org {s.get('targetOrg')!r} space "
        f"{s.get('targetSpace', target_space.strip())!r} as {s.get('snapshot')!r} "
        f"with {s.get('count', 0)} node(s). Select that org and load it with "
        "praxis_load_snapshot."
    )


@mcp.tool()
def praxis_list_org_sources() -> str:
    """List the org's spaces and their snapshots you can fold in (the Sources panel).

    Every space in the active org is org-shared: any member may browse and copy any
    space's snapshots. Returns each space and its snapshot names + node counts. Use
    ``praxis_browse_snapshot`` to inspect a snapshot's facts, then ``praxis_fold_in``
    to copy chosen facts into your working memory.
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/org/sources",
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    sources = resp.json().get("sources", [])
    if not sources:
        return "No org sources found."
    lines = [f"{len(sources)} space(s):"]
    for s in sources:
        lines.append(f"\n[{s.get('space')}]")
        snaps = s.get("snapshots") or []
        if not snaps:
            lines.append("    (no snapshots)")
        for sn in snaps:
            lines.append(f"    {sn.get('snapshot')} — {sn.get('count')} node(s)")
    return "\n".join(lines)


@mcp.tool()
def praxis_browse_snapshot(snapshot: str, space: str | None = None) -> str:
    """Browse a space snapshot's facts before folding them in (the browse view).

    Lists the facts in ``space``'s snapshot ``snapshot``, grouped into folders by
    scope, with each fact's id and text. Get ``space``/``snapshot`` from
    ``praxis_list_org_sources``; pass the fact ids you want to ``praxis_fold_in``.
    ``space`` defaults to the one selected with ``praxis_select_space``. Returns a
    structured JSON block with the grouped facts.
    """
    if (hint := _not_ready()) is not None:
        return hint
    space = _resolve_space(space)
    if not space:
        return "Pass a space (or set a default with praxis_select_space)."
    if not snapshot.strip():
        return "Pass a non-empty snapshot name."
    snapshot = snapshot.strip()
    try:
        resp = httpx.get(
            f"{identity.api_base()}/spaces/{space}/snapshots/{snapshot}/facts",
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return "Unknown space/snapshot — check praxis_list_org_sources."
        return _friendly(exc)
    payload = resp.json()
    groups = payload.get("groups", [])
    total = sum(len(g.get("facts", [])) for g in groups)
    return _structured(
        f"{total} fact(s) in snapshot {snapshot!r} from space {space!r} "
        f"across {len(groups)} folder(s).",
        payload,
    )


@mcp.tool()
def praxis_fold_in(
    snapshot: str,
    fact_ids: list[str],
    space: str | None = None,
    mode: str = "add",
) -> str:
    """Copy selected space-snapshot facts into your working memory (the "fold in").

    Folds the facts ``fact_ids`` from ``space``'s ``snapshot`` into your working
    memory: they are deduped against your facts and value conflicts are flagged
    (never silently overwritten). ``mode="add"`` (default) merges into your existing
    working memory; ``mode="replace"`` truncates it first. ``space`` defaults to the
    one selected with ``praxis_select_space``. Get the ids from
    ``praxis_browse_snapshot``. Confirm with the user first.
    """
    if (hint := _not_ready()) is not None:
        return hint
    space = _resolve_space(space)
    if not space:
        return "Pass a space (or set a default with praxis_select_space)."
    if not snapshot.strip():
        return "Pass a non-empty snapshot name."
    mode = mode.strip().lower()
    if mode not in ("add", "replace"):
        return "mode must be 'add' or 'replace'."
    if not fact_ids:
        return "Pass a non-empty list of fact_ids (see praxis_browse_snapshot)."
    try:
        resp = httpx.post(
            f"{identity.api_base()}/fold-in",
            json={
                "space": space,
                "snapshot": snapshot.strip(),
                "factIds": fact_ids,
                "mode": mode,
            },
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return "No matching space/snapshot/facts — check praxis_browse_snapshot."
        return _friendly(exc)
    payload = resp.json()
    conflicts = payload.get("conflicts", [])
    return _structured(
        f"Folded {payload.get('folded', 0)} new fact(s), deduped "
        f"{payload.get('deduped', 0)}, flagged {len(conflicts)} conflict(s) ({mode}).",
        payload,
    )


@mcp.tool()
def praxis_list_mounts() -> str:
    """List your mounted snapshots — read-only overlays added to retrieval.

    A mounted snapshot's facts are included when you read (``praxis_get_context``)
    but are NOT merged into your working memory and are NOT carried over when you
    save a snapshot. Any org-shared snapshot (identified by its ``space``/``snapshot``)
    can be mounted.
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/mounts",
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    mounts = resp.json().get("mounts", [])
    if not mounts:
        return "No snapshots are mounted."
    lines = [f"{len(mounts)} mounted snapshot(s):"]
    for m in mounts:
        lines.append(
            f"  {m.get('space')}/{m.get('snapshot')} — {m.get('count')} node(s)"
        )
    return "\n".join(lines)


@mcp.tool()
def praxis_mount_snapshot(snapshot: str, space: str | None = None) -> str:
    """Mount a space snapshot as a read-only overlay (adds it to what reads recall).

    Once mounted, ``praxis_get_context`` also recalls this snapshot's facts —
    without merging them into your working memory and without them being carried over
    on a save. Identify the snapshot by its ``space``/``snapshot`` (from
    ``praxis_list_org_sources`` / ``praxis_list_snapshots``); ``space`` defaults to
    the one selected with ``praxis_select_space``. Idempotent.
    """
    if (hint := _not_ready()) is not None:
        return hint
    space = _resolve_space(space)
    if not space:
        return "Pass a space (or set a default with praxis_select_space)."
    if not snapshot.strip():
        return "Pass a snapshot name."
    try:
        resp = httpx.post(
            f"{identity.api_base()}/mounts",
            json={"space": space, "snapshot": snapshot.strip()},
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return (
                "Unknown space or snapshot — check praxis_list_org_sources / "
                "praxis_list_snapshots."
            )
        return _friendly(exc)
    m = resp.json()
    return f"Mounted snapshot {m.get('space', space)}/{m.get('snapshot')} for reads."


@mcp.tool()
def praxis_unmount_snapshot(snapshot: str, space: str | None = None) -> str:
    """Unmount a read-only snapshot overlay (stops including it in reads).

    Identify the snapshot by its ``space``/``snapshot``; ``space`` defaults to the
    one selected with ``praxis_select_space``. No-op if it was not mounted.
    """
    if (hint := _not_ready()) is not None:
        return hint
    space = _resolve_space(space)
    if not space:
        return "Pass a space (or set a default with praxis_select_space)."
    if not snapshot.strip():
        return "Pass a snapshot name."
    try:
        resp = httpx.request(
            "DELETE",
            f"{identity.api_base()}/mounts",
            json={"space": space, "snapshot": snapshot.strip()},
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    m = resp.json()
    return f"Unmounted snapshot {m.get('space', space)}/{m.get('snapshot')}."


@mcp.tool()
def praxis_login(email: str, password: str, org_id: str | None = None) -> str:
    """Log in to Praxis with the user's email + password (the HUMAN connect path).

    TWO WAYS TO CONNECT — pick by who is running:
      • HUMAN (interactive): THIS tool. Call it when the user asks to log in /
        connect / sign in, or when another tool reports "not logged in". Ask the
        user for their credentials in chat first (their password authenticates with
        Cognito and is never stored — only a refresh token is cached). Then select
        the org with ``praxis_select_org``.
      • AGENT / automation (durable, no login): do NOT call this. Set the env var
        ``PRAXIS_API_KEY`` to a long-lived, org-scoped ``pxk_`` key and pin
        ``PRAXIS_ORG`` to that key's org (both in ``<project>/.claude/settings.local.json``).
        The MCP tools then send that key on every request — it takes precedence
        over any login, survives restarts, and needs no refresh token. Mint one from
        the dashboard (API Keys) or ``POST /apikeys`` / ``python -m knowledge.serve.apikeys mint``.
        A key only works for ITS org (wrong org → 403). Confirm either mode with ``praxis_whoami``.

    Pass ``org_id`` if the user names a specific org; otherwise a single org is
    auto-selected and multiple orgs are listed for the user to choose.
    """
    try:
        tenant, orgs = identity.authenticate(email, password)
    except Exception as exc:  # noqa: BLE001 - report any auth failure to the user
        return f"Login failed: {exc}"
    if org_id:
        identity.set_org(org_id)
        return f"Logged in as {tenant.email}; active org set to '{org_id}'."
    if tenant.org_id:
        return f"Logged in as {tenant.email}; active org '{tenant.org_id}'."
    if orgs:
        listing = ", ".join(identity.org_id_of(o) for o in orgs)
        return (
            f"Logged in as {tenant.email}. You belong to: {listing}. "
            "Call `praxis_select_org` with the one to use."
        )
    return (
        f"Logged in as {tenant.email}. You have no orgs yet — call "
        "`praxis_create_org` (you set its password) or `praxis_join_org`."
    )


@mcp.tool()
def praxis_select_org(org_id: str) -> str:
    """Set the active org for subsequent get_context / add_insight calls.

    FAILS LOUD if a ``PRAXIS_ORG`` env pin contradicts the requested org: the pin wins for every
    header (``active_org``), so silently writing a different value to the cache would make whoami and
    the actual writes disagree — a wrong-org footgun. We refuse and name both instead.
    """
    if not identity.is_logged_in():
        return "Not logged in — call `praxis_login` first."
    pin = identity.pinned_org()
    if pin and pin != org_id.strip():
        return (
            f"Org mismatch — refusing to select '{org_id}'. This project pins org='{pin}' via "
            f"PRAXIS_ORG, which wins for every request (X-Praxis-Org); you asked for '{org_id}'. "
            f"Align them: to work in '{pin}', just proceed (it is already active — do NOT select a "
            f"different org); to switch to '{org_id}', unset PRAXIS_ORG (or repin it to '{org_id}') "
            f"first. Selecting here without that would only change the cache while writes keep hitting "
            f"'{pin}' — the silent wrong-org split this guard exists to prevent."
        )
    identity.set_org(org_id)
    return f"Active org set to '{org_id}'."


def _org_action(path: str, payload: dict, org_id: str) -> str:
    try:
        resp = httpx.post(
            f"{identity.api_base()}/{path}",
            json=payload,
            headers={"Authorization": f"Bearer {identity.token()}"},
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text or exc.response.reason_phrase
        return f"Failed ({exc.response.status_code}): {detail}"
    identity.set_org(org_id)
    return f"Done; active org set to '{org_id}'."


@mcp.tool()
def praxis_create_org(org_id: str, password: str, name: str | None = None) -> str:
    """Create a new Praxis org (you set its join password) and select it."""
    if not identity.is_logged_in():
        return "Not logged in — call `praxis_login` first."
    return _org_action("orgs", {"orgId": org_id, "name": name, "password": password}, org_id)


@mcp.tool()
def praxis_join_org(org_id: str, password: str) -> str:
    """Join an existing Praxis org with its password and select it."""
    if not identity.is_logged_in():
        return "Not logged in — call `praxis_login` first."
    return _org_action("orgs/join", {"orgId": org_id, "password": password}, org_id)


@mcp.tool()
def praxis_delete_org(org_id: str) -> str:
    """Permanently delete an entire org and ALL of its data — owner-only, destructive.

    This wipes the org for EVERY member: all members' live graphs, cached snapshots,
    mounts, and API keys are purged, then the org (and its memberships and spaces) is
    removed. Only an org *owner* may do this. There is no undo. Confirm explicitly
    with the user before calling — this is far more destructive than ``praxis_clear_graph``
    (which only clears your own graph). Use ``praxis_select_org`` afterward to switch
    to another org.
    """
    if not identity.is_logged_in():
        return "Not logged in — call `praxis_login` first."
    if not org_id.strip():
        return "Pass a non-empty org_id (see praxis_whoami)."
    org_id = org_id.strip()
    try:
        resp = httpx.delete(
            f"{identity.api_base()}/orgs/{org_id}",
            headers={"Authorization": f"Bearer {identity.token()}"},
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"Unknown org {org_id!r} — you are not a member (see praxis_whoami)."
        if exc.response.status_code == 403:
            return f"Only an owner can delete org {org_id!r} — you are not its owner."
        return _friendly(exc)
    return f"Deleted org {org_id!r} and all of its data. Select another org with praxis_select_org."


@mcp.tool()
def praxis_create_space(space_id: str, name: str | None = None) -> str:
    """Create an org-shared *space* — a project folder holding snapshots.

    A space is a purely organizational, ORG-SHARED folder: every member of the active
    org can read every space and its snapshots (there is no owner and no per-user
    partitioning). It holds a collection of snapshots for one project. ``space_id`` is
    a short slug you pick (lowercase letters/digits/dash/underscore; ``"default"``,
    ``"__evals__"``, and anything with ``:`` are reserved). ``coding-validation``,
    ``building-validation``, ``planning-validation``, ``build-plan`` and any ``<x>-plan``
    slug are ALSO reserved — those are per-scope snapshot roles inside a project space, not
    standalone spaces. This does NOT change your working memory or select anything — use
    ``praxis_select_space`` to set a local
    default for the ``space`` parameter of snapshot / mount ops.

    AGENT-FACTORY PROJECTS — do NOT slugify the name. For a space that the ``/af-`` skills
    will use, ``space_id`` MUST EQUAL the factory's bare PROJECT name character for character:
    the repo directory basename VERBATIM (underscores and case preserved), or ``FACTORY_PROJECT``
    when it is pinned. ``_ticket_state.project_ref`` resolves every plan/check read to
    ``space == the bare project name``, so a hyphen-slugified space (``acme-store`` for a repo at
    ``.../acme_store``) is a SILENT failure: the space exists, but no gate ever reads it and the
    first ``/af-intake-plan`` dies with ``unknown space 'acme_store'``. The Praxis ORG id is
    separately slugified to hyphens — org and space names legitimately differ; do not reuse one
    for the other.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if not space_id.strip():
        return "Pass a non-empty space_id (a slug you pick)."
    try:
        resp = httpx.post(
            f"{identity.api_base()}/spaces",
            json={"spaceId": space_id, "name": name},
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            return f"Space {space_id!r} already exists — list spaces with praxis_list_space."
        if exc.response.status_code == 400:
            return f"Invalid space id {space_id!r}: {exc.response.text}"
        return _friendly(exc)
    return f"Created org-shared space {space_id!r}."


@mcp.tool()
def praxis_list_space() -> str:
    """List every org-shared space in the active org.

    Every space in the org is shared and readable by all members (see
    ``praxis_create_space``). Returns each space's id, name, and creation time, and
    marks the one you have set as the local default via ``praxis_select_space``.
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/spaces",
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    spaces = resp.json().get("spaces", [])
    default = identity.active_space()
    if not spaces:
        return "No spaces in this org yet — create one with praxis_create_space."
    suffix = f" (default: {default!r})" if default else ""
    lines = [f"{len(spaces)} space(s){suffix}:"]
    for s in spaces:
        sid = s.get("space_id") or s.get("spaceId")
        marker = " *" if sid == default else ""
        name = s.get("name")
        label = f" — {name}" if name else ""
        created = s.get("created_at") or s.get("createdAt")
        when = f" (created {created})" if created else ""
        lines.append(f"  {sid}{label}{when}{marker}")
    return "\n".join(lines)


@mcp.tool()
def praxis_select_space(space_id: str) -> str:
    """Set a local default ``space`` for snapshot / mount ops (client-side only).

    This does NOT touch your working memory or send any header — working-memory tools
    always resolve to your authenticated principal. It just records a client-side
    default that fills in the ``space`` parameter of the snapshot / mount / space
    tools (``praxis_save_snapshot``, ``praxis_load_snapshot``, ``praxis_mount_snapshot``,
    …) when you omit it. Pass ``""`` to clear the default (then pass ``space``
    explicitly on those calls).
    """
    if not identity.is_logged_in():
        return "Not logged in — call `praxis_login` first."
    space = space_id.strip()
    identity.set_space(space)
    if not space:
        return "Cleared the default space; pass `space` explicitly on snapshot/mount ops."
    return f"Default space set to {space!r} for snapshot/mount ops."


@mcp.tool()
def praxis_delete_space(space_id: str) -> str:
    """Permanently delete an org-shared space and ALL of its snapshots.

    This is destructive: it removes the space and every snapshot stored in it (and
    unmounts them for any viewers). It touches NO working memory — nobody's private
    live graph is affected. Because spaces are org-shared, this removes the folder for
    EVERY member of the org. Confirm with the user before calling — there is no undo.
    If it was your local default space, the default is cleared.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if not space_id.strip():
        return "Pass a non-empty space_id (see praxis_list_space)."
    space_id = space_id.strip()
    try:
        resp = httpx.delete(
            f"{identity.api_base()}/spaces/{space_id}",
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"Unknown space {space_id!r} — list spaces with praxis_list_space."
        return _friendly(exc)
    if identity.active_space() == space_id:
        identity.set_space("")
        return f"Deleted space {space_id!r} and all its snapshots; cleared it as your default."
    return f"Deleted space {space_id!r} and all its snapshots."


@mcp.tool()
def praxis_copy_space_to_org(
    target_org: str, target_space: str, space: str | None = None
) -> str:
    """Copy ALL of a space's snapshots into a NEW space in another org.

    Shares an entire project folder between two orgs the SAME login belongs to: every
    snapshot in ``space`` is copied into a brand-new space ``target_space`` in
    ``target_org``. ``space`` defaults to the one selected with ``praxis_select_space``.
    ``target_space`` is a slug you pick (lowercase letters/digits/dash/underscore;
    ``"default"`` / ``"__evals__"`` / ``:`` reserved). The copy never overwrites: it
    fails if that space already exists in ``target_org``. After it succeeds, switch
    with ``praxis_select_org(target_org)`` then load its snapshots. To share a single
    snapshot instead of the whole space, use ``praxis_copy_snapshot_to_org``.
    """
    if (hint := _not_ready()) is not None:
        return hint
    space = _resolve_space(space)
    if not space:
        return "Pass a space (or set a default with praxis_select_space)."
    if not target_org.strip():
        return "Pass a target_org you belong to (see praxis_whoami)."
    if not target_space.strip():
        return "Pass a non-empty target_space (a slug you pick for the new space)."
    try:
        resp = httpx.post(
            f"{identity.api_base()}/spaces/copy-to-org",
            json={
                "space": space,
                "targetOrg": target_org.strip(),
                "targetSpace": target_space.strip(),
            },
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"Unknown space {space!r} — list spaces with praxis_list_space."
        if exc.response.status_code == 409:
            return (
                f"Space {target_space!r} already exists in {target_org!r}. "
                "Pick a new target_space (copies never overwrite a space)."
            )
        if exc.response.status_code == 400:
            return f"Invalid target_space {target_space!r}: {exc.response.text}"
        if exc.response.status_code == 403:
            return (
                f"You are not a member of org {target_org!r} — join it first "
                "(praxis_join_org) or pick another (praxis_whoami)."
            )
        return _friendly(exc)
    s = resp.json()
    return (
        f"Copied space {space!r} into org {s.get('targetOrg')!r} as new space "
        f"{s.get('targetSpace') or s.get('space')!r} "
        f"({s.get('snapshots', s.get('count', 0))} snapshot(s)). Switch with "
        "praxis_select_org then load its snapshots."
    )


@mcp.tool()
def praxis_whoami() -> str:
    """Report the current login + active org (and the user's orgs)."""
    if _auth_disabled():
        return (
            f"auth-disabled dev mode: principal 'dev-user', org {_dev_org()!r} "
            "(no login required)."
        )
    if _api_key():
        # Durable org-scoped key auth (no Cognito login needed) — report the key's org.
        try:
            org = _key_org()
        except RuntimeError as exc:
            return f"API-key auth (PRAXIS_API_KEY) but {exc}"
        return (
            f"API-key auth (PRAXIS_API_KEY) — org: {org!r}. This durable pxk_ key is scoped to that "
            "one org; it takes precedence over any Cognito login."
        )
    if not identity.is_logged_in():
        return "Not logged in — call `praxis_login`."
    tenant = identity.load_identity()
    try:
        orgs = identity.list_my_orgs()
        listing = ", ".join(identity.org_id_of(o) for o in orgs) or "(none)"
    except Exception:  # noqa: BLE001
        listing = "(could not fetch)"
    # Report the org actually sent as X-Praxis-Org (what add_insight/facts_by hit), not the raw
    # cached org_id — a PRAXIS_ORG pin overrides the cache, and reporting the cache would lie.
    org = identity.active_org() or "(none selected)"
    pin = identity.pinned_org()
    note = ""
    if pin and pin != tenant.org_id:
        note = f" (pinned by PRAXIS_ORG; cached selection '{tenant.org_id or '(none)'}' is overridden)"
    return f"{tenant.email} — active org: {org}{note}; member of: {listing}."


@mcp.tool()
def praxis_ensure_surface(
    project: str,
    screen_id: str,
    title: str | None = None,
    file: str | None = None,
    states: list[str] | None = None,
) -> str:
    """Ensure a wireframe *surface* (a screen) exists as a fact in the graph.

    A surface is one screen of the clickable wireframe, modeled as a fact so it can
    be an endpoint of a typed ``renders`` edge from a requirement. Idempotent on
    ``(project, screen_id)`` — at most one surface fact per screen — so calling this
    twice just merge-updates the title/file/states. Usually you call
    ``praxis_bind_surface`` instead (which ensures + edges in one step); use this
    directly only to register a screen with no requirement yet.

    Returns ``{"id","project","screenId"}``.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if not project or not screen_id:
        return "Pass both a project and a screen_id."
    body: dict[str, object] = {"project": project, "screenId": screen_id}
    if title is not None:
        body["title"] = title
    if file is not None:
        body["file"] = file
    if states is not None:
        body["states"] = states
    try:
        resp = httpx.post(
            f"{identity.api_base()}/surfaces",
            json=body,
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    s = resp.json()
    return f"Ensured surface id={s.get('id')} (project={s.get('project')}, screen={s.get('screenId')})."


@mcp.tool()
def praxis_bind_surface(
    requirement_fact_id: str,
    screen_id: str,
    project: str,
    title: str | None = None,
    file: str | None = None,
    states: list[str] | None = None,
    space: str | None = None,
    snapshot: str | None = None,
) -> str:
    """Bind a requirement fact to a wireframe surface via a typed ``renders`` edge.

    Pass BOTH ``space`` and ``snapshot`` to write the binding into an org-shared snapshot
    (so a surface-bound check/requirement authored into a project snapshot resolves at build);
    omit both for working memory. Use the SAME ``(space, snapshot)`` the bound fact lives in.

    This is the PRIMARY write of the requirement<->surface factory: it ensures the
    surface fact for ``(project, screen_id)`` exists (creating/merge-updating it from
    ``title``/``file``/``states``) and edges ``requirement_fact_id -> surface`` so the
    screen is governed by that requirement. Idempotent. Use this to wire the clickable
    wireframe to the requirements that drive each screen — the bidirectional
    completeness gate (``praxis_surface_coverage``) reads these edges to find screens
    with no requirement and requirements with no screen. The requirement fact must
    already exist (find ids via ``praxis_list_graph`` / ``praxis_get_context``).

    Returns ``{"requirementFactId","surfaceId","screenId"}``.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if not requirement_fact_id or not screen_id or not project:
        return "Pass a requirement_fact_id, screen_id, and project."
    body: dict[str, object] = {
        "requirementFactId": requirement_fact_id,
        "screenId": screen_id,
        "project": project,
    }
    if title is not None:
        body["title"] = title
    if file is not None:
        body["file"] = file
    if states is not None:
        body["states"] = states
    try:
        resp = httpx.post(
            f"{identity.api_base()}/surfaces/bind",
            json=body,
            headers=_headers(space, snapshot),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    b = resp.json()
    return (
        f"Bound requirement {b.get('requirementFactId')} -> surface {b.get('surfaceId')} "
        f"(screen={b.get('screenId')})."
    )


@mcp.tool()
def praxis_unbind_surface(requirement_fact_id: str, screen_id: str, project: str) -> str:
    """Remove the ``renders`` edge between a requirement and a wireframe surface.

    Detaches ``requirement_fact_id`` from the surface for ``(project, screen_id)`` so
    that requirement no longer governs that screen. The surface fact itself is left
    in place (other requirements may still render it). Idempotent — a no-op if no
    such edge exists.

    Returns ``{"requirementFactId","screenId","project","ok":true}``.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if not requirement_fact_id or not screen_id or not project:
        return "Pass a requirement_fact_id, screen_id, and project."
    body = {
        "requirementFactId": requirement_fact_id,
        "screenId": screen_id,
        "project": project,
    }
    try:
        resp = httpx.post(
            f"{identity.api_base()}/surfaces/unbind",
            json=body,
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    u = resp.json()
    return (
        f"Unbound requirement {u.get('requirementFactId')} from screen "
        f"{u.get('screenId')} (project={u.get('project')})."
    )


@mcp.tool()
def praxis_requirements_for_surface(project: str, screen_id: str) -> str:
    """List the requirements that govern a wireframe screen (PRIMARY read).

    Answers "which requirements drive screen ``screen_id``?" — the factory query for
    going from a clickable wireframe screen back to the active requirement facts edged
    (``renders``) to it for ``(project, screen_id)``, newest first. Rejected endpoints
    drop out automatically (active-only).

    Returns a human summary plus a structured JSON block with ``requirements`` — one
    fact view per governing requirement.
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/surfaces/{screen_id}/requirements",
            params={"project": project},
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    reqs = payload.get("requirements", [])
    return _structured(
        f"{len(reqs)} requirement(s) govern screen {screen_id}."
        if reqs
        else f"No requirements govern screen {screen_id}.",
        {"project": project, "screenId": screen_id, "requirements": reqs},
    )


@mcp.tool()
def praxis_checks_for_surface(
    project: str, screen_id: str, scope: str | None = None
) -> str:
    """List ALL coverage checks bound to a wireframe screen (EXHAUSTIVE, not a sample).

    The surface-scoped completeness query for the coverage spine: every active
    ``check`` fact edged (``renders``) to ``(project, screen_id)`` — the generalization
    of ``praxis_requirements_for_surface`` to checks. Pass ``scope`` ("planning" |
    "validation") to narrow to one gate (matches ``meta.scope``). Unlike
    ``praxis_get_context`` (semantic top-k, which samples), this returns EVERY bound
    check so a per-part coverage gate never silently drops one. Active-only.

    Returns a human summary plus a structured JSON block with ``checks`` — one fact
    view per bound check.
    """
    if (hint := _not_ready()) is not None:
        return hint
    params: dict[str, str] = {"project": project}
    if scope is not None:
        params["scope"] = scope
    try:
        resp = httpx.get(
            f"{identity.api_base()}/surfaces/{screen_id}/checks",
            params=params,
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    checks = payload.get("checks", [])
    return _structured(
        f"{len(checks)} check(s) bound to screen {screen_id}."
        if checks
        else f"No checks bound to screen {screen_id}.",
        {"project": project, "screenId": screen_id, "scope": scope, "checks": checks},
    )


@mcp.tool()
def praxis_facts_by(
    category: str | None = None,
    source: str | None = None,
    scope: str | None = None,
    state: str = "active",
    meta_filter: dict | None = None,
    fields: str = "full",
    space: str | None = None,
    snapshot: str | None = None,
) -> str:
    """Enumerate ALL facts matching structured filters (EXHAUSTIVE — no top-k, no ranking).

    Pass BOTH ``space`` and ``snapshot`` to enumerate an org-shared snapshot instead of
    working memory — e.g. verify a check landed with
    ``facts_by(category="check", space=<project>, snapshot="building-validation")`` (exactly
    where af-build's RESOLVE reads). A fact written to a snapshot is NOT in working memory.

    The completeness primitive for "pull everything related to one part and enforce it".
    ``praxis_get_context`` is a semantic top-k that SAMPLES (it can silently drop a
    match) — unsafe for a forcing/completeness guarantee; this returns EVERY matching
    fact in one server-side query. Filters (all optional, AND-combined): ``category``
    (e.g. "check"), ``source``, ``scope`` (the top-level scope COLUMN — not
    ``meta.scope``), ``state`` (default "active"; pass "any" to span all states), and
    ``meta_filter`` — a ``{key: value}`` object matched against the JSONB ``meta``
    column, each key by scalar equality OR array-membership (so ``applies_to`` may be a
    single tag or a list). Example: ``category="check"`` with
    ``meta_filter={"scope":"validation","applies_to":"auth"}``.

    READING PROVENANCE: every fact view carries its COMPLETE ``meta.auditTrail`` — no
    entry cap and no truncation on this path, in working memory or in a snapshot. (A
    trail is bounded once at WRITE time, and an elision always leaves a visible
    ``action="compacted"`` entry saying how many were dropped, so a shortened trail is
    never silently short.) Because an exhaustive read of a real plan snapshot returns
    every requirement's full text (~1.2 MB across 170 tickets, enough to overrun a
    context), pass ``fields="provenance"`` to get identity + ``auditTrail`` +
    ``auditTrailCount`` per fact and nothing else — the cheap way to audit history
    across a whole snapshot. ``fields="full"`` (default) returns the whole fact.

    Returns a human summary plus a structured JSON block with ``facts`` — one fact view
    per match.
    """
    if (hint := _not_ready()) is not None:
        return hint
    params: dict[str, str] = {"state": state}
    if fields and fields != "full":
        params["fields"] = fields
    if category is not None:
        params["category"] = category
    if source is not None:
        params["source"] = source
    if scope is not None:
        params["scope"] = scope
    if meta_filter:
        params["meta"] = json.dumps(meta_filter)
    try:
        resp = httpx.get(
            f"{identity.api_base()}/facts/by",
            params=params,
            headers=_headers(space, snapshot),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    facts = payload.get("facts", [])
    return _structured(
        f"{len(facts)} fact(s) match." if facts else "No facts match the given filters.",
        {
            "category": category,
            "source": source,
            "scope": scope,
            "state": state,
            "metaFilter": meta_filter or {},
            "facts": facts,
        },
    )


@mcp.tool()
def praxis_surfaces_for_requirement(requirement_fact_id: str) -> str:
    """List the wireframe screens a requirement governs (the reverse lookup).

    Answers "which screens does requirement ``requirement_fact_id`` render?" — the
    active surface facts edged (``renders``) from this requirement. Pairs with
    ``praxis_requirements_for_surface`` to walk the requirement<->surface mapping in
    both directions.

    Returns a human summary plus a structured JSON block with ``surfaces`` — one fact
    view per governed surface.
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/facts/{requirement_fact_id}/surfaces",
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    surfaces = payload.get("surfaces", [])
    return _structured(
        f"{len(surfaces)} surface(s) governed by {requirement_fact_id}."
        if surfaces
        else f"No surfaces governed by {requirement_fact_id}.",
        {"factId": requirement_fact_id, "surfaces": surfaces},
    )


@mcp.tool()
def praxis_list_surface_bindings(project: str) -> str:
    """List every requirement<->surface binding in a project.

    Returns all ``renders`` edges whose surface belongs to ``project`` — the full
    wiring of the clickable wireframe to its requirements. Use it to audit or export
    the mapping.

    Returns a human summary plus a structured JSON block with ``bindings`` — one entry
    per edge (``requirementId``/``surfaceId``/``screenId``).
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/surfaces/bindings",
            params={"project": project},
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    bindings = payload.get("bindings", [])
    return _structured(
        f"{len(bindings)} requirement<->surface binding(s) in {project}."
        if bindings
        else f"No requirement<->surface bindings in {project}.",
        {"project": project, "bindings": bindings},
    )


@mcp.tool()
def praxis_surface_coverage(project: str, scope: str | None = None) -> str:
    """Report the bidirectional completeness gate for a project's wireframe.

    Cross-checks requirements against surfaces both ways: ``uncoveredSurfaces`` are
    screens with no requirement governing them (built but unspecified), and
    ``uncoveredRequirements`` are requirements with no screen rendering them (specified
    but unbuilt). Pass ``scope`` (e.g. ``"mvp"``) to limit the requirement side to that
    scope. Use this as the gate before declaring a wireframe complete against its PRD.

    Returns a human summary plus a structured JSON block with ``uncoveredSurfaces`` and
    ``uncoveredRequirements`` — fact views.
    """
    if (hint := _not_ready()) is not None:
        return hint
    params: dict[str, str] = {"project": project}
    if scope is not None:
        params["scope"] = scope
    try:
        resp = httpx.get(
            f"{identity.api_base()}/surfaces/coverage",
            params=params,
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    surfaces = payload.get("uncoveredSurfaces", [])
    reqs = payload.get("uncoveredRequirements", [])
    return _structured(
        f"{len(surfaces)} uncovered surface(s) and {len(reqs)} uncovered requirement(s) "
        f"in {project}.",
        {
            "project": project,
            "uncoveredSurfaces": surfaces,
            "uncoveredRequirements": reqs,
        },
    )


@mcp.tool()
def praxis_incomplete_requirements(project: str) -> str:
    """List the project's requirements that are NOT yet built/verified-complete.

    Completeness is DERIVED from verification signals, never a self-set flag: a
    requirement is incomplete if it has never had a successful outcome (never-built),
    its most recent outcome was a failure after a prior success (regressed — the
    bug/ticket path), or a fact it derives from changed (stale — needs rework). Use
    this to pick the next requirement to build and to re-find regressed ones after a
    ticket records a failed outcome.

    Returns a human summary plus a JSON block with ``incomplete`` — one entry per
    requirement (``id``/``text``/``state``/``source``/``scope``/``category``/``meta``
    plus ``reason``/``reasons``/``successCount``/``failureCount``/``lastOutcome``).
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/requirements/incomplete",
            params={"project": project},
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    incomplete = payload.get("incomplete", [])
    return _structured(
        f"{len(incomplete)} incomplete requirement(s) in {project}."
        if incomplete
        else f"All active requirements in {project} are verified-complete.",
        {"project": project, "incomplete": incomplete},
    )


@mcp.tool()
def praxis_regress_requirements(project: str, ids: list[str]) -> str:
    """Re-enter a SET of tickets into ``incomplete_requirements`` in ONE call.

    Records a failure outcome AND stamps ``build_state="incomplete"`` on every id in a
    single bulk write, so re-entering a whole plan (e.g. after grafting a new build check)
    is one round-trip instead of two-per-ticket — use this instead of looping
    ``praxis_record_outcome`` + edit over dozens of tickets (that path times out). Targets
    the project's canonical ``prd-<project>`` plan snapshot automatically, the SAME graph
    completeness derives from; confirm with ``praxis_incomplete_requirements(project)``.

    Regress by STATE only: never touch ``pinned_checks`` or the claim lease — af-build
    re-pins the fresh check set at each ticket's next start. Returns the ids regressed.
    """
    if (hint := _not_ready()) is not None:
        return hint
    ids = [str(i).strip() for i in (ids or []) if str(i).strip()]
    if not ids:
        return "Pass a non-empty list of requirement fact ids to regress."
    try:
        resp = httpx.post(
            f"{identity.api_base()}/requirements/regress",
            json={"project": project, "ids": ids},
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    regressed = payload.get("regressed", [])
    return _structured(
        f"Regressed {len(regressed)} requirement(s) in {project} back to incomplete.",
        payload,
    )


@mcp.tool()
def praxis_completeness_summary(project: str) -> str:
    """Done-of-definition counts for a project's active requirements.

    Returns totals (``total_active_requirements``/``complete``/``incomplete``) and a
    ``breakdown`` of incomplete by reason (``never_built``/``stale``/``regressed``),
    all derived from verification + staleness — no self-set completeness flag.
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/requirements/completeness",
            params={"project": project},
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    return _structured(
        f"{payload.get('complete', 0)}/{payload.get('total_active_requirements', 0)} "
        f"requirement(s) complete in {project}.",
        payload,
    )


@mcp.prompt(title="Log in to Praxis")
def login() -> str:
    """Log in to the Praxis knowledge graph (drives the praxis_login tool).

    Exposed as an MCP prompt so it shows up as a slash command
    (``/mcp__praxis__login``) for anyone who registers this server — no project
    ``.claude/commands`` file needed.
    """
    return (
        "Log me into the Praxis MCP server so `praxis_get_context` / "
        "`praxis_add_insight` work.\n\n"
        "1. Ask me for my Praxis email and password (do not guess them).\n"
        "2. Call the `praxis_login` tool with them (and `org_id` if I name one).\n"
        "3. If I belong to multiple orgs, list them and call `praxis_select_org`; "
        "if I belong to none, offer `praxis_create_org` (I set a join password) or "
        "`praxis_join_org` (needs its password).\n"
        "4. Confirm the final state with `praxis_whoami`.\n\n"
        "My password is only used to authenticate with Cognito — a refresh token "
        "is cached, never the password."
    )


def main(argv: list[str] | None = None) -> None:
    """Serve the MCP over stdio. Login is via the in-session tools/prompt, not the CLI."""
    load_dotenv()
    mcp.run()
