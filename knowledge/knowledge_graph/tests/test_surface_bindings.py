"""DB-gated unit tests for the requirement<->surface RENDERS relation.

Exercises the surface methods added to ``PostgresVectorGraph`` directly against
Postgres (SQL-only, so they cannot live in the offline write_policy tests).
Mirrors ``test_postgres_vector_graph.py``: same ``skipif`` on a resolvable DSN, a
fresh per-test tenant (``unique_org`` + a deterministic user), and a graph built
with ``FakeEmbedder`` and a ``[Redactor, Deduper]`` policy (no overwriter, so the
distinct requirement texts coexist instead of folding into one another).

A surface is modeled AS A FACT (``category="surface"``) so it can be a
``fact_edges`` endpoint; the binding is a typed ``renders`` edge
(requirement -> surface). Active-only queries mean a rejected/deleted endpoint
drops from every result with no stale hook.
"""

from __future__ import annotations

import pytest

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
    RENDERS_EDGE,
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


def _graph(conn, org, user):
    """Fresh tenant + a coexist-friendly graph (distinct texts never merge)."""
    # Edges first (FK), then facts — a clean slate every run.
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


def _surface_count(conn, org, user) -> int:
    row = conn.execute(
        "SELECT count(*) FROM facts WHERE org_id = %s AND user_id = %s "
        "AND category = 'surface'",
        (org, user),
    ).fetchone()
    return row[0] if row else 0


def test_ensure_surface_is_idempotent_per_screen(unique_org):
    """Same (project, screen_id) returns the SAME id and never a duplicate fact."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)

    first = graph.ensure_surface(PROJECT, "s-home", title="Home")
    again = graph.ensure_surface(PROJECT, "s-home", title="Home (renamed)")

    assert first == again
    assert _surface_count(conn, unique_org, USER) == 1
    # The merge-update path keeps the single fact's meta current.
    surface = graph.get_fact(first)
    assert surface.category == "surface"
    assert surface.meta.get("screen_id") == "s-home"


def test_bind_surface_is_idempotent(unique_org):
    """Binding the same pair twice yields exactly one RENDERS edge."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    req = graph.write("The home screen lists today's tasks.", state="active", category="requirement")

    s1 = graph.bind_surface(req, "s-home", PROJECT, title="Home")
    s2 = graph.bind_surface(req, "s-home", PROJECT, title="Home")

    assert s1 == s2
    renders = [e for e in graph.all_edges(RENDERS_EDGE)]
    assert renders == [(req, s1, RENDERS_EDGE)]


def test_requirements_for_surface_and_inverse(unique_org):
    """``requirements_for_surface`` returns the bound active requirement; the
    ``surfaces_for_requirement`` inverse returns the bound surface."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    req = graph.write("The home screen lists today's tasks.", state="active", category="requirement")
    other = graph.write("Settings let the user change the theme.", state="active", category="requirement")
    surface_id = graph.bind_surface(req, "s-home", PROJECT, title="Home")

    reqs = graph.requirements_for_surface(PROJECT, "s-home")
    assert [f.id for f in reqs] == [req]
    assert other not in [f.id for f in reqs]

    surfaces = graph.surfaces_for_requirement(req)
    assert [f.id for f in surfaces] == [surface_id]
    assert all(f.category == "surface" for f in surfaces)


def test_deleting_requirement_cascades_its_binding(unique_org):
    """Deleting a requirement fact drops its RENDERS edge (ON DELETE CASCADE), so
    the surface's requirement query goes empty."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    req = graph.write("The home screen lists today's tasks.", state="active", category="requirement")
    graph.bind_surface(req, "s-home", PROJECT, title="Home")
    assert [f.id for f in graph.requirements_for_surface(PROJECT, "s-home")] == [req]

    graph.delete_fact(req)

    assert graph.requirements_for_surface(PROJECT, "s-home") == []
    assert graph.all_edges(RENDERS_EDGE) == []


def test_rejecting_requirement_drops_it_and_uncovers_surface(unique_org):
    """A rejected requirement leaves the binding edge but is no longer ``active``,
    so it drops from ``requirements_for_surface`` and the now-orphaned surface
    shows up in ``surface_coverage`` uncoveredSurfaces (active-only queries; no
    stale hook needed)."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    req = graph.write("The home screen lists today's tasks.", state="active", category="requirement")
    surface_id = graph.bind_surface(req, "s-home", PROJECT, title="Home")
    assert [f.id for f in graph.requirements_for_surface(PROJECT, "s-home")] == [req]

    graph.set_state(req, "rejected")

    assert graph.requirements_for_surface(PROJECT, "s-home") == []
    coverage = graph.surface_coverage(PROJECT)
    assert surface_id in [f.id for f in coverage["uncoveredSurfaces"]]


def test_surface_coverage_flags_uncovered_surface_and_mvp_requirement(unique_org):
    """Coverage is bidirectional: an active surface with no binding is uncovered,
    and an active MVP requirement (``meta.scope == "mvp"``) with no binding is an
    uncovered requirement under the ``scope="mvp"`` filter."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)

    # A surface no requirement renders.
    orphan_surface = graph.ensure_surface(PROJECT, "s-orphan", title="Orphan")
    # An MVP requirement that renders no surface.
    mvp_req = graph.write(
        "The MVP must support offline mode.",
        state="active",
        category="requirement",
        meta={"scope": "mvp"},
    )
    # A non-MVP requirement that must NOT appear under the mvp filter.
    later_req = graph.write(
        "A later release adds dark mode.",
        state="active",
        category="requirement",
        meta={"scope": "later"},
    )

    coverage = graph.surface_coverage(PROJECT, scope="mvp")
    uncovered_surface_ids = [f.id for f in coverage["uncoveredSurfaces"]]
    uncovered_req_ids = [f.id for f in coverage["uncoveredRequirements"]]

    assert orphan_surface in uncovered_surface_ids
    assert mvp_req in uncovered_req_ids
    assert later_req not in uncovered_req_ids


def test_list_surface_bindings_returns_screen_mapping(unique_org):
    """``list_surface_bindings`` reports every RENDERS edge for the project with the
    screen id pulled from the surface meta."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    req = graph.write("The home screen lists today's tasks.", state="active", category="requirement")
    surface_id = graph.bind_surface(req, "s-home", PROJECT, title="Home")

    bindings = graph.list_surface_bindings(PROJECT)
    assert bindings == [
        {"requirementId": req, "surfaceId": surface_id, "screenId": "s-home"}
    ]


def test_unbind_surface_removes_edge(unique_org):
    """``unbind_surface`` removes the RENDERS edge; the requirement query empties."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    req = graph.write("The home screen lists today's tasks.", state="active", category="requirement")
    graph.bind_surface(req, "s-home", PROJECT, title="Home")
    assert graph.all_edges(RENDERS_EDGE)

    graph.unbind_surface(req, "s-home", PROJECT)

    assert graph.all_edges(RENDERS_EDGE) == []
    assert graph.requirements_for_surface(PROJECT, "s-home") == []


@pytest.fixture
def unique_org(request):
    # Unique per test node so reruns and parallel tenants never collide.
    return "test_" + request.node.name
