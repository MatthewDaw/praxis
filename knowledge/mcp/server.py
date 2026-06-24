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

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from knowledge.mcp import identity

mcp = FastMCP("praxis-knowledge")

_AUTH_HINT = (
    "authentication failed — ask me to log in again with `praxis_login`, or check "
    "you are a member of the active org."
)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {identity.token()}",
        "X-Praxis-Org": identity.active_org(),
    }


def _friendly(exc: httpx.HTTPStatusError) -> str:
    """Map auth failures to a clear hint; re-raise everything else."""
    if exc.response.status_code in (401, 403):
        return _AUTH_HINT
    raise exc


def _not_ready() -> str | None:
    """A guidance string when we can't call the backend yet, else ``None``.

    Lets the data tools fail soft (telling Claude how to get the user logged in /
    an org selected) instead of raising, so login is fully chat-driven.
    """
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
def praxis_get_context(query: str, top_k: int = 8) -> str:
    """Retrieve relevant stored knowledge for the current task.

    Call this before answering questions about the user's preferences,
    conventions, or past decisions — it returns active facts from the user's
    knowledge graph most similar to ``query``.

    Returns a human summary plus a structured JSON block with ``context`` and
    per-hit ``hits`` (each with ``id``/``text``/``score``/``source``/``scope``/
    ``category``) so callers can consume provenance without regex-parsing.
    """
    if (hint := _not_ready()) is not None:
        return hint
    try:
        resp = httpx.get(
            f"{identity.api_base()}/context",
            params={"query": query, "top_k": top_k},
            headers=_headers(),
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
def praxis_add_insight(
    insight: str,
    scope: str | None = None,
    category: str | None = None,
    source: str | None = None,
) -> str:
    """Store a durable insight in the user's knowledge graph.

    Before calling, push the user to state a single specific, self-contained
    insight (one that stands on its own without surrounding chat context), and
    confirm the *exact* wording with them first — that confirmation is the human
    approval gate. The insight is stored fully approved (full credibility) and
    overwrites any conflicting fact already on record, so only state what the
    user has explicitly approved.
    """
    if (hint := _not_ready()) is not None:
        return hint
    body: dict[str, object] = {"insight": insight}
    if scope is not None:
        body["scope"] = scope
    if category is not None:
        body["category"] = category
    if source is not None:
        body["source"] = source
    try:
        resp = httpx.post(
            f"{identity.api_base()}/insights",
            json=body,
            headers=_headers(),
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    return _structured(
        payload.get("summary", "") or "insight stored",
        {
            "summary": payload.get("summary", ""),
            "action": payload.get("action"),
            "id": payload.get("id"),
        },
    )


@mcp.tool()
def praxis_ingest(
    text: str,
    source: str | None = None,
    state: str = "active",
) -> str:
    """Ingest a raw document through Praxis's distillation pipeline.

    Unlike ``praxis_add_insight`` (one already-distilled fact), this hands a raw
    document (a note, a transcript, a file's contents) to the backend, which
    distills it into atomic facts, dedupes, and reconciles conflicts. ``state``
    is "active" (live knowledge) or "proposed" (staged for review). Returns a
    structured JSON block with per-document results (``id``/``action``).
    """
    if (hint := _not_ready()) is not None:
        return hint
    body: dict[str, object] = {
        "documents": [{"text": text, "source": source}],
        "state": state,
    }
    try:
        resp = httpx.post(
            f"{identity.api_base()}/ingest",
            json=body,
            headers=_headers(),
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    payload = resp.json()
    return _structured(
        f"ingested {payload.get('count', 0)} document(s)",
        payload,
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
        resp = httpx.get(f"{identity.api_base()}/contradictions", headers=_headers())
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
    keep_id: str | None = None,
    custom_text: str | None = None,
) -> str:
    """Resolve a flagged contradiction pair (from ``praxis_get_contradictions``).

    Pass either ``keep_id`` — the id of the side to keep (the other is superseded)
    — or ``custom_text`` to replace both sides with a single reconciled fact.
    Confirm the choice with the user before calling; resolution mutates the graph.
    """
    if (hint := _not_ready()) is not None:
        return hint
    if not keep_id and not (custom_text and custom_text.strip()):
        return "Pass keep_id (the side to keep) or custom_text (a reconciled fact)."
    body: dict[str, object] = {}
    if custom_text and custom_text.strip():
        body["customText"] = custom_text
    else:
        body["keepId"] = keep_id
    try:
        resp = httpx.post(
            f"{identity.api_base()}/contradictions/{pair_id}/resolve",
            json=body,
            headers=_headers(),
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
            f"{identity.api_base()}/candidates", params=params, headers=_headers()
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
def praxis_insert_fact(title: str, content: str, provenance: str | None = None) -> str:
    """Insert a fact directly into the graph, bypassing the ingestion pipeline.

    This is a *raw* write — no redaction, dedup, or conflict handling — and the
    fact lands in the "proposed" state for review. For normal human-approved
    knowledge that should reconcile with existing facts, use ``praxis_add_insight``
    (which runs the full ingestion pipeline and lands active) instead.
    """
    if (hint := _not_ready()) is not None:
        return hint
    body: dict[str, object] = {"title": title, "content": content}
    if provenance is not None:
        body["provenance"] = provenance
    try:
        resp = httpx.post(
            f"{identity.api_base()}/candidates", json=body, headers=_headers()
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
) -> str:
    """Edit an existing fact in place (find its id via ``praxis_list_graph``).

    Pass only the fields to change — ``title``, ``content``, and/or
    ``provenance``. Confirm edits with the user first; this mutates stored
    knowledge.
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
    if not body:
        return "Nothing to edit — pass title, content, and/or provenance."
    try:
        resp = httpx.patch(
            f"{identity.api_base()}/candidates/{cid}", json=body, headers=_headers()
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _friendly(exc)
    c = resp.json()
    return f"Edited fact id={c.get('id')} (state={c.get('state')})."


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
def praxis_whoami() -> str:
    """Report the current login + active org (and the user's orgs)."""
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
