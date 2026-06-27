"""Serve-level specs for FILTERED similarity on ``GET /context``.

The route gains optional positive filters (``category``/``categories``/``scope``/
``meta``) that narrow the similarity-ranked read to a subset; absent params behave
exactly as before. Like the sibling DB tests these need a Postgres DSN, but we
monkeypatch the default embedder to ``FakeEmbedder`` so the route runs fully offline
(no OPENROUTER key) and deterministically — filtering is exact regardless of the
embedder; only the ranking depends on it.
"""

from __future__ import annotations

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

import knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph as pvg  # noqa: E402
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


@pytest.fixture
def env(unique_org, monkeypatch):
    # Make every PostgresVectorGraph (incl. the route's live_graph) embed with the
    # deterministic fake, so /context needs no network/key and is reproducible.
    monkeypatch.setattr(pvg, "OpenRouterEmbedder", FakeEmbedder)
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
        conn, org, USER, embedder=FakeEmbedder(), recall_floor=-1.0,
        policy=[Redactor(), Deduper()],
    )
    yield client, graph
    for tbl in tables:
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    conn.close()


def _cats(hits) -> set[str]:
    return {h["category"] for h in hits}


def test_category_filter(env):
    client, graph = env
    graph.write("reject expired tokens", state="active", category="check")
    graph.write("the login screen", state="active", category="requirement")

    res = client.get("/context", params={"query": "anything", "category": "check"})
    assert res.status_code == 200, res.text
    hits = res.json()["hits"]
    assert hits and _cats(hits) == {"check"}


def test_categories_csv_filter(env):
    client, graph = env
    graph.write("a check", state="active", category="check")
    graph.write("a requirement", state="active", category="requirement")
    graph.write("a note", state="active", category="note")

    res = client.get(
        "/context", params={"query": "anything", "categories": "check,requirement"}
    )
    assert res.status_code == 200, res.text
    assert _cats(res.json()["hits"]) == {"check", "requirement"}


def test_meta_filter(env):
    client, graph = env
    graph.write("planning check", state="active", category="check",
                meta={"scope": "planning"})
    graph.write("validation check", state="active", category="check",
                meta={"scope": "validation"})

    res = client.get(
        "/context",
        params={"query": "a check", "category": "check", "meta": '{"scope": "planning"}'},
    )
    assert res.status_code == 200, res.text
    hits = res.json()["hits"]
    assert [h["text"] for h in hits] == ["planning check"]
    # The context blob is derived from the filtered hits, so it stays consistent.
    assert "validation check" not in res.json()["context"]


def test_no_filter_parity(env):
    """No filter params -> hits span all categories (unchanged behavior)."""
    client, graph = env
    graph.write("one", state="active", category="check")
    graph.write("two", state="active", category="requirement")

    res = client.get("/context", params={"query": "one"})
    assert res.status_code == 200, res.text
    assert _cats(res.json()["hits"]) == {"check", "requirement"}


def test_bad_meta_json_is_400(env):
    client, _graph = env
    res = client.get("/context", params={"query": "x", "meta": "not json"})
    assert res.status_code == 400
    assert "meta" in res.json()["detail"].lower()
