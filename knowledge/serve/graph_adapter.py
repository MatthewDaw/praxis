"""Serialize dashboard candidates into the graph snapshot contract."""

from __future__ import annotations

from typing import Any

from knowledge.serve.store import Candidate, contradiction_ids


def graph_from_candidates(candidates: list[Candidate]) -> dict[str, Any]:
    """Build a graph payload from the currently served candidate rows."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    seen_edges: set[str] = set()
    scope_members: dict[str, list[str]] = {}

    for candidate in candidates:
        cid = str(candidate.get("id", ""))
        if not cid:
            continue

        node: dict[str, Any] = {
            "id": cid,
            "label": str(candidate.get("title") or cid),
            "state": str(candidate.get("state") or "proposed"),
            "confidence": float(candidate.get("confidence") or 0),
        }
        for key in ("scope", "category", "provenance"):
            value = candidate.get(key)
            if value is not None:
                node[key] = str(value)
        if candidate.get("cluster_id") is not None:
            node["cluster_id"] = int(candidate["cluster_id"])
        if candidate.get("cluster_label"):
            node["cluster_label"] = str(candidate["cluster_label"])
        nodes.append(node)

        scope = candidate.get("scope")
        if scope:
            scope_members.setdefault(str(scope), []).append(cid)

        for rival_id in contradiction_ids(candidate):
            if not rival_id:
                continue
            a, b = sorted((cid, rival_id))
            key = f"contradiction:{a}__{b}"
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append({"src": cid, "dst": rival_id, "kind": "contradiction"})

    scope_groups = [
        {
            "id": scope,
            "label": scope,
            "parentId": None,
            "memberIds": sorted(member_ids),
        }
        for scope, member_ids in sorted(scope_members.items())
    ]

    graph: dict[str, Any] = {"nodes": nodes, "edges": edges}
    if scope_groups:
        graph["scopeGroups"] = scope_groups
    return graph
