"""Integration tests for the Postgres-backed vector graph.

Skipped unless a database is reachable (PRAXIS_DB_URL or a resolvable Secrets
Manager DSN). Each test uses a unique org_id so runs never collide. Tests drive
*through the trio* — ``ingestor.ingest`` then ``reader.read`` — the same path the
backend and evals use, asserting the persistence/dedup/overwrite behavior.
"""

from __future__ import annotations

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)


def _count(conn, org, user) -> int:
    row = conn.execute(
        "SELECT count(*) FROM facts WHERE org_id = %s AND user_id = %s",
        (org, user),
    ).fetchone()
    return row[0] if row else 0


def _trio(conn, org, user):
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.write_step_variants import (
        ConflictOverwriter,
        Deduper,
        Redactor,
    )
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder
    from knowledge.llm.llm_variants.fake_llm import FakeLlm
    from knowledge.wiring import build_trio

    # Fresh tenant each run (org id is derived from the test name, not random).
    conn.execute("DELETE FROM facts WHERE org_id = %s AND user_id = %s", (org, user))
    graph = PostgresVectorGraph(
        conn,
        org,
        user,
        embedder=FakeEmbedder(),
        # FakeEmbedder only scores identical text as similar, so cross-text
        # contradictions sit at ~0 similarity; recall_floor=-1.0 opts them into the
        # shared recall pass so the overwriter (whose FakeLlm always says "yes")
        # fires deterministically offline. Real runs use the default floor with
        # semantic embeddings.
        recall_floor=-1.0,
        policy=[Redactor(), Deduper(), ConflictOverwriter(llm=FakeLlm(default="yes"))],
    )
    return build_trio(graph=graph, llm=None)


def test_persist_and_retrieve(unique_org):
    conn = db.connect()
    graph, ingestor, reader = _trio(conn, unique_org, "u1")
    # Retrieval only surfaces "active" facts, so ingest as a direct approval.
    ingestor.ingest("use uv, not pip, in this repo", state="active")
    assert _count(conn, unique_org, "u1") == 1
    out = reader.read("how do I install dependencies?")
    assert "uv" in out


def test_near_dup_bumps_observation_count(unique_org):
    conn = db.connect()
    graph, ingestor, reader = _trio(conn, unique_org, "u1")
    ingestor.ingest("use uv, not pip, in this repo")
    ingestor.ingest("use uv, not pip, in this repo")  # exact dup -> merge
    assert _count(conn, unique_org, "u1") == 1
    row = conn.execute(
        "SELECT observation_count FROM facts WHERE org_id = %s AND user_id = %s",
        (unique_org, "u1"),
    ).fetchone()
    assert row[0] == 2


def test_contradiction_overwrites_in_place(unique_org):
    conn = db.connect()
    graph, ingestor, reader = _trio(conn, unique_org, "u1")
    ingestor.ingest("use uv, not pip, in this repo")
    # A contradicting add (LLM stub says "yes") force-overwrites the prior fact.
    ingestor.ingest("use pip, not uv, in this repo")
    assert _count(conn, unique_org, "u1") == 1  # row count stays 1
    row = conn.execute(
        "SELECT text, confidence FROM facts WHERE org_id = %s AND user_id = %s",
        (unique_org, "u1"),
    ).fetchone()
    assert row[0] == "use pip, not uv, in this repo"  # newest truth wins
    assert row[1] == 1.0


def test_active_facts_is_the_retrieval_graph(unique_org):
    conn = db.connect()
    graph, ingestor, reader = _trio(conn, unique_org, "u1")
    ingestor.ingest("use uv, not pip, in this repo", state="active")
    # Only "active" facts are "in the graph"; a staged (proposed) row must not show.
    # Inserted raw so the overwrite-happy test stub can't fold it into the active one.
    conn.execute(
        "INSERT INTO facts (id, org_id, user_id, text, state) VALUES (%s, %s, %s, %s, 'proposed')",
        ("staged1", unique_org, "u1", "staged note awaiting review"),
    )

    facts = graph.active_facts()
    assert [f.text for f in facts] == ["use uv, not pip, in this repo"]
    assert all(f.state == "active" for f in facts)
    # No edges are written yet, so the edge snapshot is empty (but reads cleanly).
    assert graph.active_edges() == []


@pytest.fixture
def unique_org(request):
    # Unique per test node so reruns and parallel tenants never collide.
    return "test_" + request.node.name
