"""Serve-level specs for the exhaustive coverage-spine read routes:
  * ``GET /facts/by`` — every fact matching column + JSONB ``meta`` filters, and
  * ``GET /surfaces/{screen_id}/checks`` — the ``check`` facts bound to a screen.

Like the sibling read-surface tests these exercise behavior ABOVE the component
layer and need a Postgres DSN; we seed via the graph object directly (no embedder
key needed — the read routes never embed).
"""

from __future__ import annotations

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (  # noqa: E402
    PostgresVectorGraph,
)
from knowledge.knowledge_graph.write_policy.write_step_variants import (  # noqa: E402
    Deduper,
    Redactor,
)
from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder  # noqa: E402
from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"
PROJECT = "demo"
SCREEN = "s-home"


@pytest.fixture
def env(unique_org):
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    tables = ("fact_edges", "facts", "cached_facts", "org_members", "orgs")
    for tbl in tables:
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", USER)
    app = create_app(conn)
    client = TestClient(app, headers={"X-Praxis-Org": org})
    graph = PostgresVectorGraph(
        conn,
        org,
        USER,
        embedder=FakeEmbedder(),
        recall_floor=-1.0,
        policy=[Redactor(), Deduper()],
    )
    yield client, graph
    for tbl in tables:
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    conn.close()


def _check(graph, text, *, state="active", **meta):
    return graph.write(text, state=state, category="check", meta=meta or None)


def test_facts_by_category_filter(env):
    client, graph = env
    c = _check(graph, "a check")
    graph.write("a requirement", state="active", category="requirement")

    res = client.get("/facts/by", params={"category": "check"})
    assert res.status_code == 200, res.text
    facts = res.json()["facts"]
    assert [f["id"] for f in facts] == [c]
    assert facts[0]["category"] == "check"


def test_facts_by_meta_filter_json(env):
    client, graph = env
    keep = _check(graph, "validation check", scope="validation")
    _check(graph, "planning check", scope="planning")

    res = client.get(
        "/facts/by",
        params={"category": "check", "meta": '{"scope": "validation"}'},
    )
    assert res.status_code == 200, res.text
    assert [f["id"] for f in res.json()["facts"]] == [keep]


def test_facts_by_state_any_spans_all(env):
    client, graph = env
    active = _check(graph, "active check")
    proposed = _check(graph, "proposed check", state="proposed")

    default = client.get("/facts/by", params={"category": "check"}).json()["facts"]
    assert {f["id"] for f in default} == {active}

    spanned = client.get(
        "/facts/by", params={"category": "check", "state": "any"}
    ).json()["facts"]
    assert {f["id"] for f in spanned} == {active, proposed}


def test_facts_by_invalid_meta_is_400(env):
    client, _graph = env
    res = client.get("/facts/by", params={"meta": "not json"})
    assert res.status_code == 400
    assert "meta" in res.json()["detail"].lower()


def test_checks_for_surface_route(env):
    client, graph = env
    c1 = _check(graph, "bound planning check", scope="planning")
    c2 = _check(graph, "bound validation check", scope="validation")
    _check(graph, "unbound check")
    graph.bind_surface(c1, SCREEN, PROJECT)
    graph.bind_surface(c2, SCREEN, PROJECT)

    res = client.get(f"/surfaces/{SCREEN}/checks", params={"project": PROJECT})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["project"] == PROJECT
    assert body["screenId"] == SCREEN
    assert {c["id"] for c in body["checks"]} == {c1, c2}

    scoped = client.get(
        f"/surfaces/{SCREEN}/checks",
        params={"project": PROJECT, "scope": "validation"},
    ).json()
    assert {c["id"] for c in scoped["checks"]} == {c2}


def test_checks_for_surface_requires_project(env):
    client, _graph = env
    res = client.get(f"/surfaces/{SCREEN}/checks")
    assert res.status_code == 400
