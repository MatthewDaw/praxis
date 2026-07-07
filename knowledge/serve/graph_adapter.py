"""Serialize dashboard candidates into the graph snapshot contract."""

from __future__ import annotations

from typing import Any

from knowledge.knowledge_graph.knowledge_graph_def import Fact


def graph_from_facts(
    facts: list[Fact], edges: list[tuple[str, str, str]]
) -> dict[str, Any]:
    """Build the graph snapshot from active facts — the store retrieval reads.

    Keeps the dashboard graph one-to-one with what MCP ``get_context`` recalls:
    the nodes are exactly the ``active`` facts, the edges are their persisted
    ``fact_edges``, and scope groups mirror the candidate-derived shape.

    Build-order dependencies live in each fact's ``meta.depends_on``, not in the
    ``fact_edges`` table, so they are materialized here as directed ``depends``
    edges (prerequisite -> dependent). An entry names its prerequisite by that
    requirement's ``meta.requirement_id`` (e.g. ``"R8"``) — the canonical key that
    new plans author — OR, for legacy snapshots whose requirements carry no
    requirement_id, by the prerequisite's raw fact id. Both resolve to the same
    node; this mirrors the build loop's ``_ticket_state._ids_of`` so the graph and
    the build DAG agree on one resolution rule. An entry is kept only when it
    resolves to a node in this graph.
    """
    nodes: list[dict[str, Any]] = []
    scope_members: dict[str, list[str]] = {}
    node_ids: set[str] = set()
    # requirement_id ("R8") -> node id, the lookup that resolves meta.depends_on.
    rid_to_id: dict[str, str] = {}

    for fact in facts:
        if not fact.id:
            continue
        node_ids.add(fact.id)
        rid = fact.meta.get("requirement_id")
        if rid:
            rid_to_id[str(rid)] = fact.id
        node: dict[str, Any] = {
            "id": fact.id,
            "label": fact.text or fact.id,
            "state": fact.state,
            "confidence": float(fact.confidence or 0),
        }
        if fact.scope:
            node["scope"] = fact.scope
        if fact.category:
            node["category"] = fact.category
        # Ticket lifecycle (build loop): a ticket IS a requirement fact, and its
        # done/not-done state lives in meta.build_state (absent => not yet built,
        # i.e. "incomplete"). Only requirement nodes carry this, so the dashboard
        # shows the done indicator for tickets alone.
        if fact.category == "requirement":
            node["isTicket"] = True
            node["buildState"] = fact.meta.get("build_state") or "incomplete"
        if fact.source:
            node["provenance"] = fact.source
        # Topic cluster (navigation-only): lets the view collapse facts into
        # labeled super-nodes. NULL cluster_id => an unclustered/noise node.
        if fact.cluster_id is not None:
            node["clusterId"] = fact.cluster_id
        if fact.cluster_label:
            node["clusterLabel"] = fact.cluster_label
        nodes.append(node)
        if fact.scope:
            scope_members.setdefault(fact.scope, []).append(fact.id)

    # Directed prerequisite -> dependent edges derived from meta.depends_on. Each
    # entry is a prerequisite's requirement_id (resolved through rid_to_id) or, for
    # legacy data with no requirement_id, its raw fact id; either way it lands on a
    # node id. Kept only when that node is in this graph.
    depends_edges: list[tuple[str, str, str]] = []
    for fact in facts:
        if not fact.id:
            continue
        for dep in fact.meta.get("depends_on") or []:
            dep_str = str(dep)
            src_id = rid_to_id.get(dep_str, dep_str)
            if src_id in node_ids:
                depends_edges.append((src_id, fact.id, "depends"))

    edge_list: list[dict[str, str]] = []
    seen_edges: set[str] = set()
    for src, dst, kind in [*edges, *depends_edges]:
        if not src or not dst:
            continue
        a, b = sorted((src, dst))
        key = f"{kind}:{a}__{b}"
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edge_list.append({"src": src, "dst": dst, "kind": kind})

    scope_groups = [
        {
            "id": scope,
            "label": scope,
            "parentId": None,
            "memberIds": sorted(member_ids),
        }
        for scope, member_ids in sorted(scope_members.items())
    ]

    graph: dict[str, Any] = {"nodes": nodes, "edges": edge_list}
    if scope_groups:
        graph["scopeGroups"] = scope_groups
    return graph
