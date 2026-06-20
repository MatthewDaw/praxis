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
        # contradictions sit at ~0 similarity; floor=0 lets the overwriter (whose
        # FakeLlm always says "yes") fire deterministically offline. Real runs use
        # the default floor with semantic embeddings.
        policy=[Redactor(), Deduper(), ConflictOverwriter(llm=FakeLlm(default="yes"), similarity_floor=-1.0)],
    )
    return build_trio(graph=graph, llm=None)


def test_persist_and_retrieve(unique_org):
    conn = db.connect()
    graph, ingestor, reader = _trio(conn, unique_org, "u1")
    ingestor.ingest("use uv, not pip, in this repo")
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


@pytest.fixture
def unique_org(request):
    # Unique per test node so reruns and parallel tenants never collide.
    return "test_" + request.node.name
