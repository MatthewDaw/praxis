"""Opt-in pagination for ``GET /requirements/incomplete`` (DB-gated).

The response can grow to 100k+ chars on a large plan. ``limit``/``offset`` let a client ask for a
bounded page while ``total`` always reports the full count — but BOTH default OFF so the gate's
completeness read (which must see the WHOLE set or it could believe a build is done when it isn't) is
unchanged. Seeds never-built (=> incomplete) requirements in working memory, exactly like the sibling
completeness specs, so no embedder key / snapshot is needed.
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
PROJECT = "page-app"
SOURCE = f"prd-{PROJECT}"
N = 12


@pytest.fixture
def client(unique_org):
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    tables = ("fact_edges", "facts", "snapshot_edges", "snapshots", "org_members", "orgs")
    for tbl in tables:
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", USER)
    app = create_app(conn)
    c = TestClient(app, headers={"X-Praxis-Org": org})
    graph = PostgresVectorGraph(conn, org, USER, embedder=FakeEmbedder(),
                                recall_floor=-1.0, policy=[Redactor(), Deduper()])
    for i in range(N):
        graph.write(f"requirement {i}: build distinct feature number {i}", state="active",
                    category="requirement", source=SOURCE)  # never-built => incomplete
    yield c
    for tbl in tables:
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    conn.close()


def _get(client, **params):
    r = client.get("/requirements/incomplete", params={"project": PROJECT, **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_default_returns_full_set_with_total(client):
    body = _get(client)
    assert body["total"] == N
    assert len(body["incomplete"]) == N  # unchanged, back-compatible full response


def test_limit_pages_the_set_but_total_is_full(client):
    body = _get(client, limit=5)
    assert body["total"] == N
    assert len(body["incomplete"]) == 5


def test_offset_windows_the_set(client):
    tail = _get(client, limit=5, offset=N - 2)
    assert tail["total"] == N
    assert len(tail["incomplete"]) == 2  # only two left past the offset
    head_ids = {i["id"] for i in _get(client, limit=5)["incomplete"]}
    tail_ids = {i["id"] for i in tail["incomplete"]}
    assert head_ids.isdisjoint(tail_ids)  # the window actually moved


def test_limit_zero_returns_none_but_reports_total(client):
    body = _get(client, limit=0)
    assert body["total"] == N and body["incomplete"] == []


def test_negative_offset_is_rejected(client):
    r = client.get("/requirements/incomplete", params={"project": PROJECT, "offset": -1})
    assert r.status_code == 400
