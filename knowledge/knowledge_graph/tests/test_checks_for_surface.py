"""DB-gated unit tests for ``PostgresVectorGraph.checks_for_surface`` — the
surface-scoped convenience over ``facts_by`` for the agent-factory coverage spine.

A check is a fact (``category="check"``) bound to a wireframe screen by the same
typed ``renders`` edge requirements use; ``checks_for_surface`` is the
generalization of ``requirements_for_surface`` to that category. It is EXHAUSTIVE
(every bound check, no top-k) and active-only. Mirrors ``test_surface_bindings.py``:
DSN ``skipif``, fresh per-test tenant, ``FakeEmbedder`` + ``[Redactor, Deduper]``.

Also guards verification point 1: ``category="check"`` facts must stay OUT of
``incomplete_requirements`` (they are tracked via the requirement they bind to, not
as phantom incompletes).
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
PROJECT = "demo"
SCREEN = "s-home"


@pytest.fixture
def unique_org(request):
    # Unique per test node so reruns and parallel tenants never collide.
    return "test_" + request.node.name


def _graph(conn, org, user):
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


def _check(graph, text, *, scope=None, **meta):
    m = dict(meta)
    if scope is not None:
        m["scope"] = scope
    return graph.write(text, state="active", category="check", meta=m or None)


def _ids(facts) -> set[str]:
    return {f.id for f in facts}


def test_returns_checks_bound_to_surface(unique_org):
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    c1 = _check(graph, "reject expired tokens")
    c2 = _check(graph, "rate-limit login attempts")
    _check(graph, "unbound check")  # exists but not edged to the screen

    graph.bind_surface(c1, SCREEN, PROJECT)
    graph.bind_surface(c2, SCREEN, PROJECT)

    got = graph.checks_for_surface(PROJECT, SCREEN)
    assert _ids(got) == {c1, c2}
    assert all(f.category == "check" for f in got)


def test_requirements_bound_to_same_surface_are_excluded(unique_org):
    """Only ``check`` facts come back — a requirement on the same screen does not."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    check = _check(graph, "auth check")
    req = graph.write("the login screen authenticates", state="active",
                      category="requirement", source=f"prd-{PROJECT}")
    graph.bind_surface(check, SCREEN, PROJECT)
    graph.bind_surface(req, SCREEN, PROJECT)

    got = graph.checks_for_surface(PROJECT, SCREEN)
    assert _ids(got) == {check}
    # The requirement is still reachable via the un-filtered primary query.
    assert req in _ids(graph.requirements_for_surface(PROJECT, SCREEN))


def test_scope_narrows_to_one_gate(unique_org):
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    planning = _check(graph, "planning-time check", scope="planning")
    validation = _check(graph, "validation-time check", scope="validation")
    graph.bind_surface(planning, SCREEN, PROJECT)
    graph.bind_surface(validation, SCREEN, PROJECT)

    assert _ids(graph.checks_for_surface(PROJECT, SCREEN, scope="validation")) == {
        validation
    }
    assert _ids(graph.checks_for_surface(PROJECT, SCREEN)) == {planning, validation}


def test_active_only_rejected_check_drops(unique_org):
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    keep = _check(graph, "kept check")
    drop = _check(graph, "dropped check")
    graph.bind_surface(keep, SCREEN, PROJECT)
    graph.bind_surface(drop, SCREEN, PROJECT)

    graph.set_state(drop, "rejected")
    assert _ids(graph.checks_for_surface(PROJECT, SCREEN)) == {keep}


def test_unknown_surface_is_empty(unique_org):
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    assert graph.checks_for_surface(PROJECT, "s-does-not-exist") == []


def test_checks_stay_out_of_incomplete_requirements(unique_org):
    """Verification point 1: a check scoped to prd-<project> is NOT a phantom incomplete."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    # A check carrying the same source a requirement would — it must still be ignored
    # by the requirement-completeness query, which filters category='requirement'.
    graph.write("a coverage check", state="active", category="check",
                source=f"prd-{PROJECT}")
    req = graph.write("the only real requirement", state="active",
                      category="requirement", source=f"prd-{PROJECT}")

    incomplete = graph.incomplete_requirements(PROJECT)
    assert [i["fact"].id for i in incomplete] == [req]
