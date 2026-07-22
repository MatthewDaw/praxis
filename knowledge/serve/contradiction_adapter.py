"""Bridge the knowledge store's contradiction signal into the candidate-api shape.

``serialize_pairs`` turns the candidates' contradiction links into the wire
shape the dashboard's GET /contradictions endpoint returns (one entry per unique
pair). ``detect`` is the live path: it runs the candidate texts through a real
``VectorGraph`` (whose write-policy includes the LLM ConflictFlagger) and returns
candidate-id pairs it flags — best-effort, so offline / no-API-key yields [].
"""

from __future__ import annotations

from typing import Any

from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import VectorGraph

Candidate = dict[str, Any]

# A slot is the normalized (subject, attribute) key a functional claim occupies.
Slot = tuple[str, str]
# Maps a fact id to every (slot, value) its functional claims occupy. A fact can
# compete on more than one slot, so each entry is a list.
SlotInfo = dict[str, list[tuple[Slot, str]]]


def _cid(c: Candidate) -> str:
    return str(c.get("id", ""))


def contradiction_links(c: Candidate) -> list[tuple[str, str]]:
    """``[(rival_id, status)]`` for a candidate, from the rich ``contradictions``
    field (``{id, status}``) or, failing that, the flat ``contradiction_ids``
    (treated as all ``pending``)."""
    rich = c.get("contradictions")
    if rich and isinstance(rich[0], dict):
        return [(str(x["id"]), str(x.get("status", "pending"))) for x in rich]
    return [(str(x), "pending") for x in (c.get("contradiction_ids") or [])]


def _summary(c: Candidate) -> dict[str, Any]:
    return {
        "id": _cid(c),
        "title": str(c.get("title", "")),
        "content": str(c.get("content", "")),
        "provenance": str(c.get("provenance", c.get("source", ""))),
        "state": str(c.get("state", "proposed")),
    }


def serialize_pairs(
    candidates: list[Candidate], *, status_filter: str | None = None
) -> list[dict[str, Any]]:
    """Unique contradiction pairs in the candidate-api shape (best id first).

    Each pair carries its ``status`` (``pending`` | ``resolved``) from the edge.
    Pass ``status_filter="pending"`` for the global pending-contradictions view
    (FR-013a); omit it to include resolved pairs too.
    """
    by_id = {_cid(c): c for c in candidates}
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in candidates:
        for rival_id, status in contradiction_links(c):
            if status_filter is not None and status != status_filter:
                continue
            rival = by_id.get(rival_id)
            if rival is None:
                continue
            a, b = sorted((_cid(c), rival_id))
            if a in (None, "") or f"{a}__{b}" in seen:
                continue
            seen.add(f"{a}__{b}")
            out.append({"id": f"{a}__{b}", "status": status, "a": _summary(by_id[a]), "b": _summary(by_id[b])})
    return out


def _member(c: Candidate, value: str) -> dict[str, Any]:
    """A summary plus the fact's value on the cluster's slot (``""`` when unknown)."""
    m = _summary(c)
    m["value"] = value
    return m


def serialize_clusters(
    candidates: list[Candidate],
    slot_info: SlotInfo | None = None,
    *,
    status_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Group contradiction pairs into one cluster per conflicting claim slot.

    Each contradiction edge is attributed to the (subject, attribute) slot its two
    facts share, and one cluster collects every fact competing on that slot. Because
    contradiction is *not* transitive across slots, a fact that competes on two
    slots (a compound rule) appears in two clusters — and two facts chained only
    through a shared neighbour never land together unless they truly share a slot.
    A pair whose endpoints share no slot (no claims stored, or claims changed)
    degrades to its own cluster of two, so a plain 2-fact conflict is unaffected.

    ``slot_info`` maps a fact id to every (slot, value) its functional claims hold;
    pass it from the claims table (or in-memory ``Fact.claims``). When empty, every
    pair degrades to its own cluster of two. ``status_filter`` is forwarded to
    :func:`serialize_pairs` (pass ``"pending"`` for the global pending view).
    """
    slot_info = slot_info or {}
    pairs = serialize_pairs(candidates, status_filter=status_filter)
    by_id = {_cid(c): c for c in candidates}

    # Every functional slot each fact holds, and its value on each.
    slots_of: dict[str, set[Slot]] = {}
    value_on: dict[tuple[str, Slot], str] = {}
    for fid, entries in slot_info.items():
        for slot, value in entries:
            slots_of.setdefault(fid, set()).add(slot)
            value_on.setdefault((fid, slot), value)

    # Attribute each pair to the slot(s) its endpoints share; slot-less pairs fall
    # back to a per-pair cluster.
    members_by_slot: dict[Slot, set[str]] = {}
    pairs_by_slot: dict[Slot, list[dict[str, Any]]] = {}
    fallback_pairs: list[dict[str, Any]] = []
    for p in pairs:
        a, b = p["a"]["id"], p["b"]["id"]
        shared = slots_of.get(a, set()) & slots_of.get(b, set())
        if shared:
            for slot in shared:
                members_by_slot.setdefault(slot, set()).update((a, b))
                pairs_by_slot.setdefault(slot, []).append(p)
        else:
            fallback_pairs.append(p)

    out: list[dict[str, Any]] = []
    for slot, ids in members_by_slot.items():
        member_ids = sorted(ids)
        members = [
            _member(by_id[mid], value_on.get((mid, slot), ""))
            for mid in member_ids
            if mid in by_id
        ]
        out.append(
            {
                "id": "__".join(member_ids),
                "slot": {"subject": slot[0], "attribute": slot[1]},
                "status": "pending",
                "members": members,
                "pairs": pairs_by_slot[slot],
            }
        )
    for p in fallback_pairs:
        a, b = sorted((p["a"]["id"], p["b"]["id"]))
        members = [_member(by_id[mid], "") for mid in (a, b) if mid in by_id]
        out.append(
            {
                "id": f"{a}__{b}",
                "slot": None,
                "status": "pending",
                "members": members,
                "pairs": [p],
            }
        )
    out.sort(key=lambda c: c["id"])
    return out


def detect(candidates: list[Candidate]) -> list[tuple[str, str]]:
    """Run candidate texts through a real VectorGraph; return flagged id pairs.

    Best-effort: the LLM contradiction check is skipped when no API key is set,
    so this returns [] offline. Maps the flagged facts back to candidate ids by
    matching the stored text.

    With a key, embed with the real OpenRouter embedder so semantically-related
    candidates actually clear the recall floor and reach the ConflictJudge — the
    default ``FakeEmbedder`` produces near-zero similarity between distinct texts,
    so the judge would never be consulted and nothing would ever flag.
    """
    import os

    embedder = None
    if os.getenv("OPENROUTER_API_KEY"):
        from knowledge.llm.embedder_variants.openrouter_embedder import OpenRouterEmbedder

        embedder = OpenRouterEmbedder()
    graph = VectorGraph(embedder=embedder)
    text_to_id: dict[str, str] = {}
    for c in candidates:
        content = str(c.get("content", "")).strip()
        if content:
            text_to_id[content] = _cid(c)
            graph.write(content)
    pairs: list[tuple[str, str]] = []
    for con in graph.contradictions():
        a = text_to_id.get(con.flagged.text.strip())
        b = text_to_id.get(con.conflicting.text.strip())
        if a and b and a != b:
            pairs.append((a, b))
    return pairs
