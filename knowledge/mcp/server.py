"""The ``praxis-knowledge`` MCP server: thin tools over the backend's HTTP API.

Each tool is a thin authenticated client — it mints a fresh Cognito ID token
from the cached login (:mod:`knowledge.mcp.identity`) and calls the backend with
``Authorization: Bearer <token>`` + ``X-Praxis-Org: <org>``. Tenancy and the
ingestion/retrieval pipeline live entirely on the backend; nothing here touches
the database.

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


def _headers() -> dict[str, str]:
    if _auth_disabled():
        # No bearer: the auth-disabled backend ignores it and uses dev-user.
        headers = {"X-Praxis-Org": _dev_org()}
    else:
        headers = {
            "Authorization": f"Bearer {identity.token()}",
            "X-Praxis-Org": identity.active_org(),
        }
    # Only send X-Praxis-Space when a named space is active; an absent/empty header
    # means the default space (user_id = the login's sub), so the existing one-graph-
    # per-login behaviour is unchanged for anyone who never selects a space.
    space = identity.active_space()
    if space:
        headers["X-Praxis-Space"] = space
    return headers


def _friendly(exc: httpx.HTTPStatusError) -> str:
    """Map auth failures to a clear hint; re-raise everything else."""
    if exc.response.status_code in (401, 403):
        return _AUTH_HINT
    raise exc


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
        listing = ", ".join(o.get("orgId") or o.get("org_id") for o in orgs) or "(none)"
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
def praxis_get_fact(cid: str) -> str:
    """Fetch one fact's full detail, including its writer-supplied ``meta``.

    ``praxis_get_context`` hits carry ``source``/``scope``/``category`` but not the
    free-form ``meta`` object (kept off the lean recall path). Use this to read a
    fact's ``meta`` (e.g. ``{"requirement_id": "R4"}``) and full audit trail back.
    Find the id via ``praxis_list_graph`` / ``praxis_get_context``.

    Returns a human summary plus a structured JSON block with the full candidate
    detail (``id``/``title``/``content``/``state``/``source``/``scope``/
    ``category``/``meta``/``auditTrail``...).
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/candidates/{cid}",
            headers=_headers(),
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
) -> str:
    """Store a durable insight in the user's knowledge graph.

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
        body["meta"] = meta
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
        return _timeout_note("add_insight")
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    summary = payload.get("summary", "") or "insight stored"
    surfaced = payload.get("contradictionsSurfaced") or 0
    if surfaced:
        summary = (
            f"{summary} — {surfaced} pending contradiction(s) raised; "
            "review with praxis_get_contradictions"
        )
    return _structured(
        summary,
        {
            "summary": payload.get("summary", ""),
            "action": payload.get("action"),
            "id": payload.get("id"),
            "onConflict": payload.get("onConflict"),
            "contradictionsSurfaced": surfaced,
        },
    )


@mcp.tool()
def praxis_add_insights(
    insights: list[dict],
    on_conflict: str = "auto_resolve",
    raw: bool = False,
) -> str:
    """Store many already-distilled insights in ONE call (bulk sibling of praxis_add_insight).

    Use this when you have several confirmed, self-contained insights to persist
    at once (e.g. the learnings from a whole session) instead of calling
    ``praxis_add_insight`` repeatedly — it's one round-trip and the backend writes
    them serially, which is both faster and gentler on the write path than firing
    many concurrent single-insight calls.

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

    Returns a structured JSON block with one result per insight (in order), each
    carrying ``ok``/``id``/``action``/``retrievable`` (read-your-writes confirmed)
    and, on a per-item failure, an ``error`` — a bad item never aborts the rest.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if on_conflict not in ("auto_resolve", "surface"):
        return "on_conflict must be 'auto_resolve' or 'surface'."
    if not isinstance(insights, list) or not insights:
        return "insights must be a non-empty list of insight objects."
    body = {"insights": insights, "onConflict": on_conflict, "raw": raw}
    try:
        resp = httpx.post(
            f"{identity.api_base()}/insights/batch",
            json=body,
            headers=_headers(),
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
    summary = f"stored {ok}/{payload.get('count', len(results))} insight(s)"
    if surfaced:
        summary += (
            f" — {surfaced} pending contradiction(s) raised; "
            "review with praxis_get_contradictions"
        )
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
def praxis_record_outcome(fact_id: str, outcome: str) -> str:
    """Feed a downstream verification result back into a fact's trust (gap H1).

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
            headers=_headers(),
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
def praxis_get_contradictions() -> str:
    """List the flagged contradictions in the user's knowledge graph.

    Each entry is a pair of facts the conflict detector judged to contradict each
    other; both are kept in the graph until resolved. Use this to review what is
    flagged and why, then call ``praxis_resolve_contradiction`` to settle a pair.
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/contradictions",
            headers=_headers(),
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
) -> str:
    """Resolve a flagged contradiction cluster (from ``praxis_get_contradictions``).

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
            headers=_headers(),
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
) -> str:
    """Insert a fact directly into the graph, bypassing the ingestion pipeline.

    This is a *raw* write — no redaction, dedup, or conflict handling — and the
    fact lands in the "proposed" state for review. For normal human-approved
    knowledge that should reconcile with existing facts, use ``praxis_add_insight``
    (which runs the full ingestion pipeline and lands active) instead.

    ``category`` tags the fact's kind (e.g. ``"requirement"``/``"learning"``);
    ``meta`` is a free-form object persisted onto the fact (structured provenance);
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
            headers=_headers(),
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
) -> str:
    """Edit an existing fact in place (find its id via ``praxis_list_graph``).

    Pass only the fields to change — ``title``, ``content``, ``provenance``,
    ``category``, ``meta`` (merged into the fact's existing meta), and/or
    ``derived_from`` (ids to attach as ``derived_from`` edges from this fact).
    Confirm edits with the user first; this mutates stored knowledge.
    """
    if (hint := _not_ready()) is not None:
        return hint
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
    try:
        resp = httpx.patch(
            f"{identity.api_base()}/candidates/{cid}",
            json=body,
            headers=_headers(),
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
def praxis_promote_fact(cid: str, target_state: str | None = None) -> str:
    """Promote a fact through its lifecycle (the dashboard "promote" action).

    Moves a fact forward one step (e.g. ``proposed`` -> ``active``); pass
    ``target_state`` to force a specific destination, or omit it to let the
    backend advance to the next state. Find the id via ``praxis_list_graph``.
    Confirm with the user first — this changes what retrieval reads.
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
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    c = resp.json()
    return f"Promoted fact id={c.get('id')} (state={c.get('state')})."


@mcp.tool()
def praxis_reject_fact(cid: str, reason: str | None = None) -> str:
    """Reject a fact (the dashboard "reject" action).

    Marks a proposed/active fact as rejected so retrieval stops reading it;
    pass an optional ``reason`` for the audit trail. Find the id via
    ``praxis_list_graph``. Confirm with the user first.
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
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    c = resp.json()
    return f"Rejected fact id={c.get('id')} (state={c.get('state')})."


@mcp.tool()
def praxis_delete_fact(cid: str) -> str:
    """Permanently delete a fact from the graph (the dashboard "delete" action).

    Unlike reject (which keeps the row in a rejected state), this removes the
    fact entirely. Find the id via ``praxis_list_graph``. Confirm with the user
    first — this is irreversible. Returns a 409 hint if the fact can't be
    deleted (e.g. it is referenced elsewhere).
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.delete(
            f"{identity.api_base()}/candidates/{cid}",
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            return f"Cannot delete fact {cid}: {exc.response.text}"
        return _friendly(exc)
    return f"Deleted fact id={cid}."


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
def praxis_list_snapshots() -> str:
    """List the caller's saved graph snapshots (the dashboard Snapshots panel).

    Each snapshot is a saved copy of the live graph you can restore later via
    ``praxis_load_snapshot``. Returns name, node count, and creation time.
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/snapshots",
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    snaps = resp.json().get("snapshots", [])
    if not snaps:
        return "No snapshots saved."
    lines = [f"{len(snaps)} snapshot(s):"]
    for s in snaps:
        lines.append(
            f"  {s.get('name')} — {s.get('count')} node(s)"
            f"{f' (saved {s.get('createdAt')})' if s.get('createdAt') else ''}"
        )
    return "\n".join(lines)


@mcp.tool()
def praxis_save_snapshot(name: str) -> str:
    """Save the current live graph as a snapshot (the dashboard "save snapshot").

    Creates or overwrites the snapshot named ``name`` with the current graph
    state, so you can restore it later with ``praxis_load_snapshot``.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if not name.strip():
        return "Pass a non-empty snapshot name."
    try:
        resp = httpx.post(
            f"{identity.api_base()}/snapshots",
            json={"name": name.strip()},
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    s = resp.json()
    return f"Saved snapshot {s.get('name')!r} with {s.get('count', 0)} node(s)."


@mcp.tool()
def praxis_load_snapshot(name: str, mode: str = "replace") -> str:
    """Restore a snapshot into the live graph (the dashboard "load snapshot").

    ``mode="replace"`` (default) truncates the live graph then loads the
    snapshot; ``mode="add"`` merges the snapshot into the current graph,
    replacing only nodes it shares by id. Confirm with the user first —
    ``replace`` discards the current graph (save it first if unsure).
    """
    if (hint := _not_ready()) is not None:
        return hint
    mode = mode.strip().lower()
    if mode not in ("add", "replace"):
        return "mode must be 'add' or 'replace'."
    try:
        resp = httpx.post(
            f"{identity.api_base()}/snapshots/{name}/load",
            json={"mode": mode},
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"Unknown snapshot {name!r} — list them with praxis_list_snapshots."
        return _friendly(exc)
    return f"Loaded {resp.json().get('loaded', 0)} node(s) from snapshot {name!r} ({mode})."


@mcp.tool()
def praxis_delete_snapshot(name: str) -> str:
    """Delete a saved snapshot (the dashboard "delete snapshot" action).

    Removes the snapshot named ``name``; the live graph is unaffected. Confirm
    with the user first.
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.delete(
            f"{identity.api_base()}/snapshots/{name}",
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    return f"Deleted snapshot {resp.json().get('deleted', name)!r}."


@mcp.tool()
def praxis_list_org_sources() -> str:
    """List org members and their snapshots you can fold in (the Sources panel).

    Within the active org any member may browse and copy another member's saved
    snapshots. Returns each member (user id, role, whether it's you) and their
    snapshot names + node counts. Use ``praxis_browse_snapshot`` to inspect a
    snapshot's facts, then ``praxis_fold_in`` to copy chosen facts into your graph.
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
    lines = [f"{len(sources)} source(s):"]
    for s in sources:
        who = s.get("username") or s.get("userId")
        tag = " (you)" if s.get("isSelf") else ""
        lines.append(f"\n[{s.get('userId')}] {who}{tag} — role {s.get('role')}")
        snaps = s.get("snapshots") or []
        if not snaps:
            lines.append("    (no snapshots)")
        for sn in snaps:
            lines.append(f"    {sn.get('name')} — {sn.get('count')} node(s)")
    return "\n".join(lines)


@mcp.tool()
def praxis_browse_snapshot(user_id: str, name: str) -> str:
    """Browse a member's snapshot facts before folding them in (the browse view).

    Lists the facts in member ``user_id``'s snapshot ``name``, grouped into
    folders by scope, with each fact's id and text. Get ``user_id``/``name``
    from ``praxis_list_org_sources``; pass the fact ids you want to
    ``praxis_fold_in``. Returns a structured JSON block with the grouped facts.
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/org/sources/{user_id}/snapshots/{name}/facts",
            headers=_headers(),
            timeout=_READ_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return "Unknown member/snapshot — check praxis_list_org_sources."
        return _friendly(exc)
    payload = resp.json()
    groups = payload.get("groups", [])
    total = sum(len(g.get("facts", [])) for g in groups)
    return _structured(
        f"{total} fact(s) in snapshot {name!r} from {user_id} across {len(groups)} folder(s).",
        payload,
    )


@mcp.tool()
def praxis_fold_in(
    source_user: str,
    snapshot: str,
    fact_ids: list[str],
    mode: str = "add",
) -> str:
    """Copy selected snapshot facts from a member into your graph (the "fold in").

    Folds the facts ``fact_ids`` from ``source_user``'s ``snapshot`` into your
    live graph: they are deduped against your facts and value conflicts are
    flagged (never silently overwritten). ``mode="add"`` (default) merges into
    your existing graph; ``mode="replace"`` truncates your graph first. Get the
    ids from ``praxis_browse_snapshot``. Confirm with the user first.
    """
    if (hint := _not_ready()) is not None:
        return hint
    mode = mode.strip().lower()
    if mode not in ("add", "replace"):
        return "mode must be 'add' or 'replace'."
    if not fact_ids:
        return "Pass a non-empty list of fact_ids (see praxis_browse_snapshot)."
    try:
        resp = httpx.post(
            f"{identity.api_base()}/fold-in",
            json={
                "sourceUser": source_user,
                "snapshot": snapshot,
                "factIds": fact_ids,
                "mode": mode,
            },
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return "No matching member/snapshot/facts — check praxis_browse_snapshot."
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
    but are NOT merged into your live graph and are NOT carried over when you save
    a snapshot. Mounts can be your own snapshots or any org member's.
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
        who = "you" if m.get("isSelf") else m.get("sourceUser")
        lines.append(f"  {m.get('snapshot')} (from {who}) — {m.get('count')} node(s)")
    return "\n".join(lines)


@mcp.tool()
def praxis_mount_snapshot(snapshot: str, source_user: str | None = None) -> str:
    """Mount a snapshot as a read-only overlay (adds it to what reads recall).

    Once mounted, ``praxis_get_context`` also recalls this snapshot's facts —
    without merging them into your live graph and without them being carried over
    on a save. ``source_user`` defaults to you (mount your own snapshot); pass an
    org member's id (from ``praxis_list_org_sources``) to mount theirs. Idempotent.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if not snapshot.strip():
        return "Pass a snapshot name."
    body: dict[str, object] = {"snapshot": snapshot.strip()}
    if source_user is not None:
        body["sourceUser"] = source_user
    try:
        resp = httpx.post(
            f"{identity.api_base()}/mounts",
            json=body,
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return (
                "Unknown member or snapshot — check praxis_list_org_sources / "
                "praxis_list_snapshots."
            )
        return _friendly(exc)
    m = resp.json()
    return f"Mounted snapshot {m.get('snapshot')!r} from {m.get('sourceUser')} for reads."


@mcp.tool()
def praxis_unmount_snapshot(snapshot: str, source_user: str | None = None) -> str:
    """Unmount a read-only snapshot overlay (stops including it in reads).

    ``source_user`` defaults to you. No-op if it was not mounted.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if not snapshot.strip():
        return "Pass a snapshot name."
    body: dict[str, object] = {"snapshot": snapshot.strip()}
    if source_user is not None:
        body["sourceUser"] = source_user
    try:
        resp = httpx.request(
            "DELETE",
            f"{identity.api_base()}/mounts",
            json=body,
            headers=_headers(),
            timeout=_WRITE_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    m = resp.json()
    return f"Unmounted snapshot {m.get('snapshot')!r} from {m.get('sourceUser')}."


@mcp.tool()
def praxis_login(email: str, password: str, org_id: str | None = None) -> str:
    """Log in to Praxis with the user's email + password (and optional org).

    Call this when the user asks to log in / connect / sign in to Praxis, or when
    another tool reports "not logged in". Ask the user for their credentials in
    chat first. (Their password is sent to this local tool to authenticate with
    Cognito; it is not stored in plaintext — only a refresh token is cached.)
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
        listing = ", ".join(o.get("orgId") or o.get("org_id") for o in orgs)
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
    """Set the active org for subsequent get_context / add_insight calls."""
    if not identity.is_logged_in():
        return "Not logged in — call `praxis_login` first."
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
    """Create a private working *space* in the active org and select it.

    A space is an independent live knowledge graph owned by your login: it lets one
    login drive MULTIPLE separate graphs in an org (e.g. different agents on different
    tasks) instead of the single default graph. ``space_id`` is a short slug you pick
    (lowercase letters/digits/dash/underscore; ``"default"`` and anything with ``:``
    are reserved). Spaces are private to the creating login. On success the new space
    becomes active locally (subsequent get_context / add_insight calls run against it,
    via the ``X-Praxis-Space`` header). Use ``praxis_select_space`` with ``""`` to
    return to the default space.
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
            return f"Space {space_id!r} already exists — select it with praxis_select_space."
        if exc.response.status_code == 400:
            return f"Invalid space id {space_id!r}: {exc.response.text}"
        return _friendly(exc)
    identity.set_space(space_id)
    return f"Created space {space_id!r}; it is now the active space."


@mcp.tool()
def praxis_list_space() -> str:
    """List the private spaces you own in the active org (and which is active).

    Each space is an independent live graph owned by your login (see
    ``praxis_create_space``). Returns each space's id, name, and creation time, and
    notes which one is currently active — ``(default)`` when no named space is
    selected. Switch with ``praxis_select_space``.
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
    active = identity.active_space()
    active_label = active or "(default)"
    if not spaces:
        return f"No named spaces yet. Active space: {active_label}."
    lines = [f"{len(spaces)} space(s) (active: {active_label}):"]
    for s in spaces:
        sid = s.get("space_id") or s.get("spaceId")
        marker = " *" if sid == active else ""
        name = s.get("name")
        label = f" — {name}" if name else ""
        created = s.get("created_at") or s.get("createdAt")
        when = f" (created {created})" if created else ""
        lines.append(f"  {sid}{label}{when}{marker}")
    return "\n".join(lines)


@mcp.tool()
def praxis_select_space(space_id: str) -> str:
    """Set the active space for subsequent get_context / add_insight calls.

    Switches the live graph this login drives to the named space (see
    ``praxis_create_space`` / ``praxis_list_space``). Pass ``""`` or ``"default"`` to
    clear back to the default space (the login's own single graph). This is local —
    it just changes the ``X-Praxis-Space`` header sent on later calls.
    """
    if not identity.is_logged_in():
        return "Not logged in — call `praxis_login` first."
    space = space_id.strip()
    if space.lower() == "default":
        space = ""
    identity.set_space(space)
    if not space:
        return "Active space cleared back to the default space."
    return f"Active space set to {space!r}."


@mcp.tool()
def praxis_delete_space(space_id: str) -> str:
    """Permanently delete one of your private spaces and its entire working graph.

    This is destructive: it purges the space's live knowledge graph (every fact,
    edge, claim, snapshot, and mount owned by that space) and removes the space
    itself. The default graph and your other spaces are untouched. Only the space's
    owning login can delete it. Confirm with the user before calling — there is no
    undo (save a snapshot first if unsure). If the deleted space was the active one,
    this falls back locally to the default space.
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
            return f"Unknown space {space_id!r} — list your spaces with praxis_list_space."
        return _friendly(exc)
    if identity.active_space() == space_id:
        identity.set_space("")
        return (
            f"Deleted space {space_id!r} and its working graph; "
            "active space fell back to the default space."
        )
    return f"Deleted space {space_id!r} and its working graph."


@mcp.tool()
def praxis_whoami() -> str:
    """Report the current login + active org (and the user's orgs)."""
    if _auth_disabled():
        return (
            f"auth-disabled dev mode: principal 'dev-user', org {_dev_org()!r} "
            "(no login required)."
        )
    if not identity.is_logged_in():
        return "Not logged in — call `praxis_login`."
    tenant = identity.load_identity()
    try:
        orgs = identity.list_my_orgs()
        listing = ", ".join(o.get("orgId") or o.get("org_id") for o in orgs) or "(none)"
    except Exception:  # noqa: BLE001
        listing = "(could not fetch)"
    org = tenant.org_id or "(none selected)"
    return f"{tenant.email} — active org: {org}; member of: {listing}."


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
) -> str:
    """Bind a requirement fact to a wireframe surface via a typed ``renders`` edge.

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
            headers=_headers(),
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
) -> str:
    """Enumerate ALL facts matching structured filters (EXHAUSTIVE — no top-k, no ranking).

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

    Returns a human summary plus a structured JSON block with ``facts`` — one fact view
    per match.
    """
    if (hint := _not_ready()) is not None:
        return hint
    params: dict[str, str] = {"state": state}
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
            headers=_headers(),
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
