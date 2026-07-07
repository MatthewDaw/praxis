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


def test_graph_from_facts_derives_depends_edges_from_meta():
    # Build-order deps live in meta.depends_on as the prerequisite's stable
    # requirement_id (NOT its fact id); they surface as directed prerequisite ->
    # dependent edges, resolved through the requirement_id -> node index.
    facts = [
        Fact(id="base", text="scaffold", state="active", category="requirement",
             meta={"requirement_id": "R1"}),
        Fact(id="models", text="models", state="active", category="requirement",
             meta={"requirement_id": "R2", "depends_on": ["R1"]}),
        Fact(
            id="persist",
            text="persistence",
            state="active",
            category="requirement",
            meta={"requirement_id": "R3", "depends_on": ["R2", "R99"]},  # R99 not a node
        ),
    ]

    graph = graph_adapter.graph_from_facts(facts, [])

    assert {"src": "base", "dst": "models", "kind": "depends"} in graph["edges"]
    assert {"src": "models", "dst": "persist", "kind": "depends"} in graph["edges"]
    # A depends_on requirement_id that is not a node in this graph is skipped.
    assert len(graph["edges"]) == 2


def test_graph_from_facts_depends_on_falls_back_to_fact_id_for_legacy():
    # Legacy snapshots (e.g. the scraper) carry no requirement_id and write the
    # prerequisite's raw fact id in depends_on. That still resolves — the renderer
    # shares the build loop's rule (requirement_id first, fact id fallback) so both
    # id schemes draw edges. An entry that is neither is dropped (no phantom edge).
    facts = [
        Fact(id="fid-base", text="base", state="active", category="requirement"),
        Fact(id="fid-dep", text="dep", state="active", category="requirement",
             meta={"depends_on": ["fid-base", "nope"]}),  # fact id resolves; "nope" does not
    ]

    graph = graph_adapter.graph_from_facts(facts, [])

    assert graph["edges"] == [{"src": "fid-base", "dst": "fid-dep", "kind": "depends"}]


def test_graph_from_facts_marks_ticket_build_state():
    # Ticket nodes (requirement facts) carry a done/not-done signal from
    # meta.build_state; non-ticket facts carry neither isTicket nor buildState.
    facts = [
        Fact(id="t1", text="finished ticket", state="active", category="requirement",
             meta={"build_state": "finished"}),
        Fact(id="t2", text="unbuilt ticket", state="active", category="requirement"),
        Fact(id="d1", text="a decision", state="active", category="episodic"),
    ]

    graph = graph_adapter.graph_from_facts(facts, [])
    by_id = {n["id"]: n for n in graph["nodes"]}

    assert by_id["t1"]["isTicket"] is True
    assert by_id["t1"]["buildState"] == "finished"
    # No build_state in meta => not-yet-built => "incomplete".
    assert by_id["t2"]["buildState"] == "incomplete"
    # Non-requirement facts are not tickets.
    assert "isTicket" not in by_id["d1"]
    assert "buildState" not in by_id["d1"]


def test_graph_from_facts_empty_has_no_scope_groups():
    graph = graph_adapter.graph_from_facts([], [])
    assert graph == {"nodes": [], "edges": []}
