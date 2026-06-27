"""DB-gated unit tests for FILTERED similarity search — `search(...)` narrowed by
category/scope/meta, and the same filters on the live+mounted `overlay_search`.

This is the SIMILARITY-ranked, filtered read (the complement of PR #113's exhaustive
`facts_by`): results stay ranked by relevance but only matching rows are considered.
SQL-only, so (like the sibling DB tests) it runs directly against Postgres with a
fresh per-test tenant, `FakeEmbedder`, and a coexist-friendly `[Redactor, Deduper]`
policy. `FakeEmbedder` maps identical text -> identical vector (cosine 1.0), so a
query equal to a fact's text ranks that fact first — that's how we assert ranking.
"""

from __future__ import annotations

import json

import pytest

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
    PostgresVectorGraph,
    _fit,
)
from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder
from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "u1"
SNAP_KEY = "snapshot:s1"


@pytest.fixture
def unique_org(request):
    return "test_" + request.node.name


def _graph(conn, org, user):
    conn.execute("DELETE FROM cached_fact_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM cached_facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM fact_edges WHERE org_id = %s AND user_id = %s", (org, user))
    conn.execute("DELETE FROM facts WHERE org_id = %s AND user_id = %s", (org, user))
    return PostgresVectorGraph(
        conn, org, user, embedder=FakeEmbedder(), recall_floor=-1.0,
        policy=[Redactor(), Deduper()],
    )


def _seed_cached(conn, org, user, fid, text, *, category=None, meta=None):
    """Seed one snapshot fact directly into cached_facts (with a fitted embedding).

    Mirrors how snapshots are populated (a copy/insert, NOT the write pipeline) —
    cached_facts has no success_count column, so it can't go through _search_vec's
    recall. The embedding is required: overlay_search filters ``embedding IS NOT NULL``.
    """
    emb = _fit(FakeEmbedder().embed([text])[0])
    conn.execute(
        "INSERT INTO cached_facts "
        "(id, org_id, user_id, cache_key, text, category, state, embedding, meta) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, %s)",
        (fid, org, user, SNAP_KEY, text, category, emb, json.dumps(meta or {})),
    )


def _texts(hits) -> list[str]:
    return [h.fact.text for h in hits]


def _cats(hits) -> set[str]:
    return {h.fact.category for h in hits}


def test_category_filter_returns_only_matching(unique_org):
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    graph.write("reject expired tokens", state="active", category="check")
    graph.write("rate-limit logins", state="active", category="check")
    graph.write("the login screen", state="active", category="requirement")

    hits = graph.search("anything relevant", top_k=10, categories=["check"])
    assert hits, "expected matches"
    assert _cats(hits) == {"check"}
    # No-filter parity: the unfiltered search DOES see the other category.
    allhits = graph.search("anything relevant", top_k=10)
    assert "requirement" in _cats(allhits)


def test_filter_stays_ranked_not_exhaustive(unique_org):
    """The matching fact most similar to the query ranks first; ranking is preserved."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    graph.write("alpha apple check", state="active", category="check")
    graph.write("zzz totally different check", state="active", category="check")

    hits = graph.search("alpha apple check", top_k=10, categories=["check"])
    assert _texts(hits)[0] == "alpha apple check"  # exact-match (cosine 1.0) leads
    # Scores are descending (ranked, not an arbitrary enumeration).
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_scope_filter(unique_org):
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    graph.write("mvp check", state="active", category="check", scope="mvp")
    graph.write("v2 check", state="active", category="check", scope="v2")

    hits = graph.search("a check", top_k=10, categories=["check"], scope="mvp")
    assert {h.fact.scope for h in hits} == {"mvp"}


def test_meta_filter_scalar_and_array(unique_org):
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    graph.write("planning check", state="active", category="check",
                meta={"scope": "planning"})
    graph.write("validation check", state="active", category="check",
                meta={"scope": "validation"})
    graph.write("multi check", state="active", category="check",
                meta={"applies_to": ["s-home", "*"]})

    plan = graph.search("a check", top_k=10, meta_filter={"scope": "planning"})
    assert _texts(plan) == ["planning check"]
    # Array-membership: "s-home" matches the list-valued applies_to.
    member = graph.search("a check", top_k=10, meta_filter={"applies_to": "s-home"})
    assert _texts(member) == ["multi check"]


def test_no_filter_parity(unique_org):
    """Absent filters -> the same result set as a plain search (characterization)."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    graph.write("one", state="active", category="check")
    graph.write("two", state="active", category="requirement")

    base = graph.search("one", top_k=10)
    none_passed = graph.search("one", top_k=10, categories=None,
                               scope=None, meta_filter=None)
    assert [h.fact.id for h in base] == [h.fact.id for h in none_passed]


def test_overlay_search_filters_live_and_mounted(unique_org):
    """Category/scope/meta filters apply to BOTH the live and mounted branches."""
    conn = db.connect()
    live = _graph(conn, unique_org, USER)
    # Live: one check + one requirement. Snapshot: one check + one note.
    live.write("live check", state="active", category="check")
    live.write("live requirement", state="active", category="requirement")
    _seed_cached(conn, unique_org, USER, "sc1", "snapshot check", category="check")
    _seed_cached(conn, unique_org, USER, "sn1", "snapshot note", category="note")

    hits = live.overlay_search(
        "anything", [(USER, SNAP_KEY)], top_k=10, categories=["check"]
    )
    texts = set(_texts(hits))
    assert texts == {"live check", "snapshot check"}  # both branches filtered to check
    assert all(h.fact.category == "check" for h in hits)
    # The mounted check is flagged as coming from the snapshot.
    mounted = [h for h in hits if h.fact.text == "snapshot check"][0]
    assert mounted.fact.meta.get("mountedFrom")
