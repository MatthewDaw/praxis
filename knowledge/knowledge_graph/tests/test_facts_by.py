"""DB-gated unit tests for ``PostgresVectorGraph.facts_by`` — the exhaustive,
server-side filtered fact enumeration behind the agent-factory coverage spine.

Unlike ``search``/``read`` (semantic top-k that SAMPLES) this returns EVERY fact
matching the given column/meta predicates — the completeness primitive. SQL-only,
so (like ``test_surface_bindings.py``) it runs directly against Postgres: same
``skipif`` on a resolvable DSN, a fresh per-test tenant, and a graph built with
``FakeEmbedder`` and a coexist-friendly ``[Redactor, Deduper]`` policy so distinct
texts never fold into one another.
"""

from __future__ import annotations

import pytest

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
    PostgresVectorGraph,
)
from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder
from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "u1"


@pytest.fixture
def unique_org(request):
    # Unique per test node so reruns and parallel tenants never collide.
    return "test_" + request.node.name


def _graph(conn, org, user):
    """Fresh tenant + a coexist-friendly graph (distinct texts never merge)."""
    conn.execute("DELETE FROM fact_edges WHERE org_id = %s AND user_id = %s", (org, user))
    conn.execute("DELETE FROM facts WHERE org_id = %s AND user_id = %s", (org, user))
    return PostgresVectorGraph(
        conn,
        org,
        user,
        embedder=FakeEmbedder(),
        recall_floor=-1.0,
        policy=[Redactor(), Deduper()],
    )


def _ids(facts) -> set[str]:
    return {f.id for f in facts}


def test_empty_result_when_nothing_matches(unique_org):
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    graph.write("a lone learning", state="active", category="learning")

    assert graph.facts_by(category="check") == []


def test_category_filter_returns_all_matches_no_topk(unique_org):
    """Every matching fact comes back — not a sampled top-k."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    checks = {
        graph.write(f"check number {i}", state="active", category="check")
        for i in range(7)
    }
    graph.write("not a check", state="active", category="requirement")

    got = graph.facts_by(category="check")
    assert _ids(got) == checks
    assert all(f.category == "check" for f in got)


def test_source_and_scope_column_filters(unique_org):
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    a = graph.write("scoped mvp", state="active", category="check",
                    source="prd-app", scope="mvp")
    graph.write("scoped v2", state="active", category="check",
                source="prd-app", scope="v2")
    graph.write("other source", state="active", category="check", source="prd-other")

    assert _ids(graph.facts_by(source="prd-app")) == _ids(
        [f for f in graph.facts_by() if f.source == "prd-app"]
    )
    # scope here is the top-level COLUMN, not meta.scope.
    assert _ids(graph.facts_by(category="check", scope="mvp")) == {a}


def test_state_filter_active_by_default_and_spannable(unique_org):
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    active = graph.write("active check", state="active", category="check")
    proposed = graph.write("proposed check", state="proposed", category="check")

    assert _ids(graph.facts_by(category="check")) == {active}  # default active-only
    assert _ids(graph.facts_by(category="check", state="proposed")) == {proposed}
    assert _ids(graph.facts_by(category="check", state=None)) == {active, proposed}


def test_meta_scalar_equality(unique_org):
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    planning = graph.write("planning check", state="active", category="check",
                           meta={"scope": "planning", "severity": "high"})
    graph.write("validation check", state="active", category="check",
                meta={"scope": "validation", "severity": "high"})

    got = graph.facts_by(category="check", meta_filter={"scope": "planning"})
    assert _ids(got) == {planning}


def test_meta_multi_key_is_anded(unique_org):
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    match = graph.write("hi planning", state="active", category="check",
                        meta={"scope": "planning", "severity": "high"})
    graph.write("lo planning", state="active", category="check",
                meta={"scope": "planning", "severity": "low"})

    got = graph.facts_by(
        category="check", meta_filter={"scope": "planning", "severity": "high"}
    )
    assert _ids(got) == {match}


def test_meta_array_membership_for_applies_to(unique_org):
    """A meta value that is a JSON array matches by MEMBERSHIP, not equality."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    listed = graph.write("multi-surface check", state="active", category="check",
                         meta={"applies_to": ["s-home", "*"]})
    scalar = graph.write("scalar check", state="active", category="check",
                         meta={"applies_to": "s-home"})
    graph.write("other surface", state="active", category="check",
                meta={"applies_to": ["s-login"]})

    got = graph.facts_by(category="check", meta_filter={"applies_to": "s-home"})
    # The array member AND the scalar both match a query for "s-home".
    assert _ids(got) == {listed, scalar}
    # The wildcard tag matches only the fact that carries it.
    assert _ids(graph.facts_by(category="check", meta_filter={"applies_to": "*"})) == {
        listed
    }
