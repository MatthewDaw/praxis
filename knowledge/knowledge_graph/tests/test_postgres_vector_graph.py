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


# --- snapshot reload (id stability + atomicity) -------------------------------
# A snapshot reload (POST /snapshots/load -> load_caches / merge_caches_into_live) is
# a natural-key-reconciled, ATOMIC upsert into working memory: repeated reloads never
# drift the count and a requirement the graph already holds keeps its live fact id
# instead of adopting the snapshot's. The reload does NOT preserve live
# working-memory-only meta — working memory is a UI/personal-memory buffer, not the
# factory's state store (factory build state lives on the prd-<project> snapshot
# itself), so a reload legitimately takes the snapshot baseline.


def _live_graph(conn, org, user):
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder

    conn.execute("DELETE FROM facts WHERE org_id = %s AND user_id = %s", (org, user))
    return PostgresVectorGraph(
        conn, org, user, embedder=FakeEmbedder(), recall_floor=-1.0,
        policy=[Redactor(), Deduper()],
    )


def _fact_by_reqid(conn, org, user, rid):
    """(id, meta) of the sole working-memory fact carrying meta.requirement_id == rid."""
    import json as _json

    rows = conn.execute(
        "SELECT id, meta FROM facts WHERE org_id=%s AND user_id=%s "
        "AND meta->>'requirement_id' = %s",
        (org, user, rid),
    ).fetchall()
    assert len(rows) == 1, f"expected exactly one R={rid} fact, got {len(rows)}"
    fid, meta = rows[0]
    return fid, (meta if isinstance(meta, dict) else _json.loads(meta))


def test_reload_takes_snapshot_baseline_not_live_working_meta(unique_org):
    """Reload takes the snapshot baseline — it does NOT preserve live working-memory-only
    meta. (Option A: working memory is a UI/personal-memory buffer, not the factory's
    state store, so there is no in-flight build state on it to protect.)"""
    conn = db.connect()
    org, user = unique_org, "u1"
    space, snap = "build-plan", "prd-shopping"
    g = _live_graph(conn, org, user)
    live_id = g.write(
        "The scraper must respect robots.txt",
        state="active", category="requirement", meta={"requirement_id": "R1"},
    )
    g.save_cache(space, snap)  # baseline meta: requirement_id only, no build_state
    fact = g.get_fact(live_id)
    g.set_meta(live_id, {**(fact.meta or {}), "build_state": "finished"})  # working-memory-only

    g.merge_caches_into_live([(space, snap)])  # the reload path

    fid, meta = _fact_by_reqid(conn, org, user, "R1")
    assert fid == live_id                 # id still stable across the reload
    assert "build_state" not in meta      # reverted to the snapshot baseline (not preserved)


def test_reload_keeps_live_id_when_snapshot_reassigned_id(unique_org):
    """The unstable-id repro: the snapshot stores the same requirement under a
    DIFFERENT fact id than the live graph (independent materialization). The reload
    must keep the live id (reconciled by meta.requirement_id), not adopt the
    snapshot's — and there must be exactly one fact for the requirement."""
    conn = db.connect()
    org, user = unique_org, "u1"
    space, snap = "build-plan", "prd-shopping"
    g = _live_graph(conn, org, user)
    live_id = g.write(
        "Rate-limit outbound requests",
        state="active", category="requirement", meta={"requirement_id": "R3"},
    )
    g.save_cache(space, snap)
    # Simulate the snapshot's copy of R3 having a different id (as if saved from a
    # separately-materialized graph): re-key the snapshot row.
    other_id = "ffffffffffffffffffffffffffffffff"
    conn.execute(
        "UPDATE snapshots SET id=%s WHERE org_id=%s AND space=%s AND snapshot=%s AND id=%s",
        (other_id, org, space, snap, live_id),
    )

    g.merge_caches_into_live([(space, snap)])

    fid, _meta = _fact_by_reqid(conn, org, user, "R3")  # asserts exactly one
    assert fid == live_id                # kept the live id, did NOT flip to other_id


def test_repeated_reload_is_idempotent_no_count_drift(unique_org):
    """Repeated reloads converge: the requirement count and its fact id stay fixed
    (guards the count-drift / duplicate-materialization regression)."""
    conn = db.connect()
    org, user = unique_org, "u1"
    space, snap = "build-plan", "prd-shopping"
    g = _live_graph(conn, org, user)
    live_id = g.write(
        "Emit a run summary",
        state="active", category="requirement", meta={"requirement_id": "R4"},
    )
    g.save_cache(space, snap)

    for _ in range(3):
        g.merge_caches_into_live([(space, snap)])
        fid, _ = _fact_by_reqid(conn, org, user, "R4")  # exactly one, every time
        assert fid == live_id


def test_snapshot_bound_outcome_drives_completeness(unique_org):
    """Option A: ticket STATE lives ON the prd-<project> snapshot. record_outcome and
    the completeness derivation both work snapshot-bound (migration 0012 gives snapshots
    the outcome-trust columns) — a recorded success flips a requirement from incomplete
    (never-built) to complete, with NO working memory in the loop."""
    conn = db.connect()
    org = unique_org
    g = _snapshot_graph(conn, org, "shopping", "prd-shopping")
    rid = g.write("Scrape the catalog", state="active", category="requirement",
                  source="prd-shopping", meta={"requirement_id": "R1"})
    assert rid is not None
    # never-built -> incomplete
    assert rid in {i["fact"].id for i in g.incomplete_requirements("shopping")}
    assert g.completeness_summary("shopping")["incomplete"] == 1
    # record a success ON THE SNAPSHOT -> complete (no working memory touched)
    g.record_outcome(rid, success=True)
    assert rid not in {i["fact"].id for i in g.incomplete_requirements("shopping")}
    assert g.completeness_summary("shopping")["complete"] == 1


# --- write-time snapshot-KIND invariant (checks may not co-mingle with a plan) ----
# The section invariant refuses, at WRITE time, a fact that violates the KIND its
# destination snapshot allows (kind derived from the name): a prd-* plan admits NO
# category="check" fact; building-/planning-validation admit ONLY matching-scope
# checks; every other name is unconstrained. This is the fix for the "11 checks
# embedded in a prd snapshot" failure — save_cache (whole-graph dump), the
# snapshot-bound _add (direct write), and copy_snapshot_from all enforce it.


def _snapshot_graph(conn, org, space, snap):
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder

    conn.execute(
        "DELETE FROM snapshots WHERE org_id=%s AND space=%s AND snapshot=%s", (org, space, snap)
    )
    return PostgresVectorGraph(
        conn, org, facts_table="snapshots", space=space, snapshot=snap,
        embedder=FakeEmbedder(), recall_floor=-1.0, policy=[Redactor(), Deduper()],
    )


def test_save_cache_rejects_check_into_prd_snapshot(unique_org):
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        SnapshotKindError,
    )

    conn = db.connect()
    org, user = unique_org, "u1"
    g = _live_graph(conn, org, user)
    g.write("A plan requirement", state="active", category="requirement",
            meta={"requirement_id": "R1"})
    g.write("A coverage check", state="active", category="check",
            meta={"scope": "validation", "applies_to": ["*"]})
    with pytest.raises(SnapshotKindError):
        g.save_cache("myproj", "prd-myproj")


def test_save_cache_allows_requirements_and_pinned_checks_meta_into_prd(unique_org):
    conn = db.connect()
    org, user = unique_org, "u1"
    g = _live_graph(conn, org, user)
    # A requirement ticket carrying a pinned_checks META key is NOT a category=check
    # fact — it saves fine (pinned_checks is the load-bearing contract, untouched).
    g.write("A plan requirement", state="active", category="requirement",
            meta={"requirement_id": "R1", "pinned_checks": [{"validation_id": "v1"}]})
    assert g.save_cache("myproj", "prd-myproj") == 1


def test_snapshot_bound_add_enforces_scope_for_validation_snapshot(unique_org):
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        SnapshotKindError,
    )

    conn = db.connect()
    org = unique_org
    g = _snapshot_graph(conn, org, "myproj", "building-validation")
    # A validation-scope check is admitted...
    assert g.write("check ok", state="active", category="check",
                   meta={"scope": "validation"}) is not None
    # ...but a planning-scope check, or a non-check, is refused.
    with pytest.raises(SnapshotKindError):
        g.write("wrong scope", state="active", category="check", meta={"scope": "planning"})
    with pytest.raises(SnapshotKindError):
        g.write("a requirement", state="active", category="requirement", meta={})


def test_snapshot_bound_add_unconstrained_name_accepts_anything(unique_org):
    conn = db.connect()
    org = unique_org
    g = _snapshot_graph(conn, org, "monica", "demo")
    assert g.write("any check", state="active", category="check",
                   meta={"scope": "planning"}) is not None


def test_copy_snapshot_from_rejects_checks_into_prd_destination(unique_org):
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
        SnapshotKindError,
    )
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder

    conn = db.connect()
    org = unique_org
    # Source: a checks-bearing snapshot (unconstrained name, so it accepts the checks).
    src = _snapshot_graph(conn, org, "shared", "lenses")
    src.write("a check", state="active", category="check", meta={"scope": "validation"})
    # Destination governs the kind: copying into a prd-* plan destination is refused.
    conn.execute(
        "DELETE FROM snapshots WHERE org_id=%s AND space=%s AND snapshot=%s",
        (org, "myproj", "prd-myproj"),
    )
    dst = PostgresVectorGraph(
        conn, org, facts_table="snapshots", space="myproj", snapshot="prd-myproj",
        embedder=FakeEmbedder(),
    )
    with pytest.raises(SnapshotKindError):
        dst.copy_snapshot_from(org, "shared", "lenses")


@pytest.fixture
def unique_org(request):
    # Unique per test node so reruns and parallel tenants never collide.
    return "test_" + request.node.name
