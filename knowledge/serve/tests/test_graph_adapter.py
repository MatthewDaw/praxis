"""Unit tests for the graph snapshot adapters (no database required)."""

from __future__ import annotations

from knowledge.knowledge_graph.knowledge_graph_def import Fact
from knowledge.serve import graph_adapter


def test_graph_from_facts_maps_nodes_scopes_and_edges():
    facts = [
        Fact(id="f1", text="use uv, not pip", state="active", confidence=1.0, scope="tooling", source="mcp"),
        Fact(id="f2", text="prefer pytest", state="active", confidence=0.8, scope="tooling"),
        Fact(id="f3", text="deploy via CDK", state="active", scope="infra", category="ops"),
    ]
    edges = [("f1", "f2", "support")]

    graph = graph_adapter.graph_from_facts(facts, edges)

    assert [n["id"] for n in graph["nodes"]] == ["f1", "f2", "f3"]
    f1 = graph["nodes"][0]
    assert f1["label"] == "use uv, not pip"
    assert f1["state"] == "active"
    assert f1["scope"] == "tooling"
    assert f1["provenance"] == "mcp"  # source -> provenance
    assert graph["nodes"][2]["category"] == "ops"
    assert graph["edges"] == [{"src": "f1", "dst": "f2", "kind": "support"}]
    groups = {g["id"]: g["memberIds"] for g in graph["scopeGroups"]}
    assert groups == {"infra": ["f3"], "tooling": ["f1", "f2"]}


def test_graph_from_facts_dedupes_undirected_edges():
    facts = [Fact(id="a", text="A", state="active"), Fact(id="b", text="B", state="active")]
    edges = [("a", "b", "contradiction"), ("b", "a", "contradiction")]

    graph = graph_adapter.graph_from_facts(facts, edges)

    assert len(graph["edges"]) == 1


def test_graph_from_facts_empty_has_no_scope_groups():
    graph = graph_adapter.graph_from_facts([], [])
    assert graph == {"nodes": [], "edges": []}
