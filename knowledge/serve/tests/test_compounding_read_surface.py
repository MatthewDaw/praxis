"""Serve-level specs for the compounding-loop READ surface exposed to MCP.

Covers the HTTP routes the MCP tools forward to:
  * ``as_of`` point-in-time recall on ``GET /context`` (item 4),
  * H5 staleness traversal: ``GET /derivations/stale`` +
    ``GET /facts/{id}/dependents`` (item 2),
  * the ``meta`` read path via ``GET /candidates/{id}`` (item 8).

Like test_episodic_memory.py these exercise behavior ABOVE the component layer and
need a Postgres DSN AND an OPENROUTER_API_KEY (the HTTP write path embeds for real).
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (  # noqa: E402
    DERIVED_FROM_EDGE,
    PostgresVectorGraph,
)
from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None or not os.getenv("OPENROUTER_API_KEY"),
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret) AND OPENROUTER_API_KEY",
)

USER = "dev-user"


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
    graph = PostgresVectorGraph(conn, org, USER)
    yield client, graph
    for tbl in tables:
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    conn.close()


def _add(client, insight, **extra):
    res = client.post("/insights", json={"insight": insight, **extra})
    assert res.status_code == 200, res.text
    return res.json()["id"]


# --- item 4: as_of point-in-time recall on /context ------------------------
def test_context_as_of_excludes_later_fact(env):
    """A fact written now must be absent from a /context recall pinned to the past."""
    client, _ = env
    insight = "The retry budget for the ingest worker is 5 attempts."
    _add(client, insight)
    query = "How many retries does the ingest worker get?"

    now = client.get("/context", params={"query": query}).json()
    assert insight in (now.get("context") or "") or now.get("hits")

    past = client.get(
        "/context", params={"query": query, "as_of": "2000-01-01T00:00:00Z"}
    ).json()
    texts = [h["text"] for h in past.get("hits", [])]
    assert insight not in texts


# --- item 2: H5 staleness traversal ----------------------------------------
def test_stale_derivations_after_source_rejected(env):
    """Rejecting a source fact flags its derived learning; the route surfaces it."""
    client, graph = env
    source_id = _add(client, "The auth service signs tokens with RS256.")
    derived_id = _add(client, "Verify JWTs with the RS256 public key in the gateway.")
    graph.add_edge(derived_id, source_id, DERIVED_FROM_EDGE)

    # Dependents traversal sees the derived learning before any invalidation.
    deps = client.get(f"/facts/{source_id}/dependents").json()["dependents"]
    assert derived_id in [d["id"] for d in deps]

    # Nothing stale yet.
    assert client.get("/derivations/stale").json()["stale"] == []

    # Reject the source via the reject path (fires the H5 hook).
    assert client.post(f"/candidates/{source_id}/reject").status_code == 200

    stale = client.get("/derivations/stale").json()["stale"]
    assert derived_id in [s["id"] for s in stale]


# --- item 8: meta read path -------------------------------------------------
def test_meta_round_trips_through_candidate_detail(env):
    """A writer-set meta object is readable back via GET /candidates/{id}."""
    client, _ = env
    meta = {"requirement_id": "R4", "owner": "matt"}
    fid = _add(client, "Requirements must be confirmed verbatim before storing.", meta=meta)

    detail = client.get(f"/candidates/{fid}").json()
    assert detail["meta"].get("requirement_id") == "R4"
    assert detail["meta"].get("owner") == "matt"
