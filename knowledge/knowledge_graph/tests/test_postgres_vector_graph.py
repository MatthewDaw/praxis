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


def _states_by_text(conn, org, user) -> dict[str, str]:
    rows = conn.execute(
        "SELECT text, state FROM facts WHERE org_id = %s AND user_id = %s",
        (org, user),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _seed_active(conn, org, user, *texts) -> list[str]:
    """Seed coexisting facts (no overwriter) and return their ids, in order."""
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder

    g = PostgresVectorGraph(
        conn, org, user, embedder=FakeEmbedder(), recall_floor=-1.0,
        policy=[Redactor(), Deduper()],  # no overwriter: distinct texts coexist
    )
    return [g.write(t, state="active") for t in texts]


def test_contradiction_keeps_both_nondestructively(unique_org):
    """FR-003/SC-001: an approved contradicting add keeps the loser (text intact,
    state rejected) and links the pair with a ``contradicted_by`` edge — the prior
    fact is never overwritten in place."""
    conn = db.connect()
    graph, ingestor, reader = _trio(conn, unique_org, "u1")
    ingestor.ingest("use uv, not pip, in this repo", state="active")
    ingestor.ingest("use pip, not uv, in this repo", state="active")

    # Both facts survive — the loser's text is preserved, not overwritten.
    assert _count(conn, unique_org, "u1") == 2
    assert _states_by_text(conn, unique_org, "u1") == {
        "use pip, not uv, in this repo": "active",   # newest approved truth
        "use uv, not pip, in this repo": "rejected",  # prior, kept intact
    }
    # Linked as resolved (contradicted_by), not left pending.
    assert len(graph.all_edges("contradicted_by")) == 1
    assert graph.all_edges("contradiction") == []


def test_active_active_contradiction_demotes_newcomer_to_proposed(unique_org):
    """FR-005: a forced-active write whose functional claim clashes with an already-
    active fact lands ``proposed`` -- a pending contradiction -- never a second active
    side. The pair stays linked. Exercises the structural detector on the postgres store."""
    from knowledge.knowledge_graph.knowledge_graph_def import Claim
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
    from knowledge.knowledge_graph.write_policy.write_step_variants import (
        ClaimConflictDetector,
        Deduper,
        Redactor,
    )
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder

    class _Claims(WriteStep):
        """Stand-in for ClaimExtractor: assigns claims by exact text match."""

        consumes_candidates = False

        def __init__(self, mapping):
            self._m = mapping

        def apply(self, decision):
            decision.claims = list(self._m.get(decision.text, []))

    mapping = {
        "the deploy timeout is 30 seconds": [
            Claim(subject="deploy", attribute="timeout", value="30", functional=True)
        ],
        "the deploy timeout is 60 seconds": [
            Claim(subject="deploy", attribute="timeout", value="60", functional=True)
        ],
    }
    conn = db.connect()
    # Fresh tenant each run (org id is derived from the test name), mirroring _trio.
    conn.execute("DELETE FROM facts WHERE org_id = %s AND user_id = %s", (unique_org, "u1"))
    graph = PostgresVectorGraph(
        conn, unique_org, "u1", embedder=FakeEmbedder(), recall_floor=-1.0,
        policy=[Redactor(), _Claims(mapping), Deduper(), ClaimConflictDetector()],
    )
    graph.write("the deploy timeout is 30 seconds", state="active")
    graph.write("the deploy timeout is 60 seconds", state="active")  # 60 != 30 -> contradiction

    states = _states_by_text(conn, unique_org, "u1")
    assert states["the deploy timeout is 30 seconds"] == "active"     # first stays live
    assert states["the deploy timeout is 60 seconds"] == "proposed"   # FR-005: not a 2nd active
    # Still linked, as a pending (unresolved) contradiction.
    assert len(graph.all_edges("contradiction")) == 1


def test_overwrite_rejects_all_conflicts_without_destroying_text(unique_org):
    """US1 #2: approving over several conflicts rejects+links each loser, none
    overwritten. Drives _overwrite directly with multiple conflicts."""
    from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder

    conn = db.connect()
    graph, _, _ = _trio(conn, unique_org, "u1")
    a1, a2 = _seed_active(conn, unique_org, "u1", "tabs for indentation", "two spaces for indentation")

    new_text = "four spaces for indentation"
    decision = WriteDecision(text=new_text, state="active")
    decision.embedding = FakeEmbedder().embed_one(new_text)
    decision.update_target_id = a1
    decision.supersede_ids = [a2]
    new_id = graph._overwrite(decision)

    states = _states_by_text(conn, unique_org, "u1")
    assert states["tabs for indentation"] == "rejected"        # text intact
    assert states["two spaces for indentation"] == "rejected"  # text intact
    assert states[new_text] == "active"
    # Both losers linked to the winner; nothing left pending.
    assert len(graph.all_edges("contradicted_by")) == 2
    assert graph.all_edges("contradiction") == []
    assert new_id not in (a1, a2)


def test_overwrite_rejects_a_proposed_conflict(unique_org):
    """US1 #3 / FR-006: a contradicted fact that was only proposed (never live) is
    moved to rejected and linked, not silently dropped."""
    from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder

    conn = db.connect()
    graph, _, _ = _trio(conn, unique_org, "u1")
    # A staged (proposed) rival, seeded raw so it never went live.
    conn.execute(
        "INSERT INTO facts (id, org_id, user_id, text, state) VALUES (%s,%s,%s,%s,'proposed')",
        ("prop1", unique_org, "u1", "legacy os.path standard"),
    )
    new_text = "pathlib is the standard"
    decision = WriteDecision(text=new_text, state="active")
    decision.embedding = FakeEmbedder().embed_one(new_text)
    decision.update_target_id = "prop1"
    new_id = graph._overwrite(decision)

    states = _states_by_text(conn, unique_org, "u1")
    assert states["legacy os.path standard"] == "rejected"  # not dropped; text intact
    assert states[new_text] == "active"
    assert len(graph.all_edges("contradicted_by")) == 1
    assert new_id != "prop1"


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


def test_hybrid_search_lifts_exact_keyword_fact_above_pure_cosine(unique_org):
    """Hybrid (vector + BM25 via RRF) ranks an exact-identifier fact strictly higher
    than pure cosine does.

    Seeds one fact carrying a rare runbook code (RBK-7782) among several "on-call
    engineer" distractors. The query names that code. With the deterministic
    FakeEmbedder the cosine branch buries the terse code fact near the bottom; the
    BM25 IDF keyword branch ranks it #1 (the code's lexemes have df=1), so fusing the
    two lifts the code fact's rank. We assert the *improvement* (robust regardless of
    embedder): hybrid rank < pure-cosine rank. ``hybrid=False`` proves the legacy
    pure-cosine path is still reachable and unchanged."""
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder

    conn = db.connect()
    conn.execute("DELETE FROM facts WHERE org_id = %s AND user_id = %s", (unique_org, "u1"))
    graph = PostgresVectorGraph(
        conn, unique_org, "u1", embedder=FakeEmbedder(),
        policy=[Redactor(), Deduper()],
    )
    keyword_fact = "Runbook entry RBK-7782: restart the queue consumer and clear the dead-letter table."
    for text in [
        keyword_fact,
        "The on-call engineer should investigate failed jobs and restart affected workers.",
        "Our deployments occasionally fail and need to be retried by an on-call engineer.",
        "When a service is unhealthy, the on-call engineer checks dashboards and error logs.",
        "On-call engineers triage production incidents and escalate when they cannot resolve them.",
        "The on-call rotation handbook explains how engineers respond to alerts and failures.",
    ]:
        graph.write(text, state="active")

    query = "What should the on-call engineer do for RBK-7782?"

    def rank_of(hits) -> int:
        return next(i for i, h in enumerate(hits) if h.fact.text == keyword_fact)

    cosine_hits = graph.search(query, top_k=6, hybrid=False)  # default path (pure cosine)
    hybrid_hits = graph.search(query, top_k=6, hybrid=True)  # opt-in keyword fusion
    assert all(h.score is not None for h in cosine_hits)
    # The keyword branch ranks the code fact #1; fusion must improve its position.
    assert rank_of(hybrid_hits) < rank_of(cosine_hits), (
        [h.fact.text for h in hybrid_hits],
        [h.fact.text for h in cosine_hits],
    )


def test_decide_is_read_only_then_persist_writes(unique_org):
    # The batch writer relies on this split: decide() must touch no rows, and
    # persist() must be the only thing that writes.
    conn = db.connect()
    graph, _, _ = _trio(conn, unique_org, "u1")
    decision = graph.decide("use uv, not pip, in this repo", state="active")
    assert decision is not None and decision.action == "add"
    assert _count(conn, unique_org, "u1") == 0  # decide() persisted nothing
    fid = graph.persist(decision)
    assert fid and _count(conn, unique_org, "u1") == 1


def test_write_equals_decide_then_persist(unique_org):
    # write() == decide() then persist(): a re-decided exact duplicate dedups into
    # the persisted fact exactly as a plain write() would (the same-batch case the
    # parallel writer hits when it re-decides on the base connection).
    conn = db.connect()
    graph, _, _ = _trio(conn, unique_org, "u2")
    first = graph.write("ship behind a feature flag", state="active")
    decision = graph.decide("ship behind a feature flag", state="active")  # exact dup
    assert decision is not None and decision.action == "update"
    assert decision.update_target_id == first
    assert graph.persist(decision) == first
    assert _count(conn, unique_org, "u2") == 1


@pytest.fixture
def unique_org(request):
    # Unique per test node so reruns and parallel tenants never collide.
    return "test_" + request.node.name
