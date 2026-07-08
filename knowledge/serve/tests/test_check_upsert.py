"""Checks are identity-keyed on meta.check_id and NEVER text-deduped/reconciled.

Regression for the bug where praxis_add_insight(category="check", on_conflict="surface")
AUTO-MERGED a new check into a prose-similar existing check — returning action="merged"
and silently dropping the new check's distinct meta.run (its executable gate). A check is a
declarative gate keyed on check_id + run, not a knowledge assertion; two checks with different
check_id/run must stay distinct even when their descriptions read alike.

Exercises _check_upsert directly on a snapshot-bound, REDACT-ONLY graph (the exact graph the
/insights endpoint builds for a check write), so no HTTP client / OPENROUTER is needed.
"""

from __future__ import annotations

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)


def _check_graph(conn, org, space, snap):
    """A snapshot-bound, redact-only graph — what the /insights check path constructs."""
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.write_step_variants import Redactor
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder

    conn.execute(
        "DELETE FROM snapshots WHERE org_id=%s AND space=%s AND snapshot=%s", (org, space, snap)
    )
    return PostgresVectorGraph(
        conn, org, facts_table="snapshots", space=space, snapshot=snap,
        embedder=FakeEmbedder(), recall_floor=-1.0, policy=[Redactor()],
    )


def _run_of(graph, check_id):
    """The meta.run stored on the check with this check_id in the graph (or None)."""
    for f in graph.facts_by(category="check", state=None, meta_filter={"check_id": check_id}):
        return (f.meta or {}).get("run")
    return None


def test_distinct_check_ids_with_similar_prose_stay_separate(unique_org):
    """Two checks with DIFFERENT check_id/run but near-identical prose -> BOTH persist,
    each keeps its own run. No merge, no dropped gate (the reported bug)."""
    from knowledge.serve.app import _check_upsert

    conn = db.connect()
    g = _check_graph(conn, unique_org, "proj", "building-validation")

    r1 = _check_upsert(g, insight="the DDL upsert path is correct",
                       source="prd-proj", scope="validation",
                       meta={"check_id": "postgres-ddl-upsert", "applies_to": ["db"],
                             "run": "pytest -k postgres_ddl_upsert"})
    r2 = _check_upsert(g, insight="the DDL upsert path is correct",  # deliberately identical prose
                       source="prd-proj", scope="validation",
                       meta={"check_id": "stable-idempotency-keys", "applies_to": ["db"],
                             "run": "pytest -k stable_idempotency_key"})

    assert r1["action"] == "added" and r2["action"] == "added"
    assert r1["id"] != r2["id"]                       # two distinct facts, never merged
    checks = g.facts_by(category="check", state=None)
    assert len(checks) == 2
    # each retains ITS OWN run selector — the new gate is not lost
    assert _run_of(g, "postgres-ddl-upsert") == "pytest -k postgres_ddl_upsert"
    assert _run_of(g, "stable-idempotency-keys") == "pytest -k stable_idempotency_key"


def test_same_check_id_updates_in_place(unique_org):
    """Re-admitting a check with an EXISTING check_id updates that one fact in place —
    no duplicate, no new id, and the new run wins."""
    from knowledge.serve.app import _check_upsert

    conn = db.connect()
    g = _check_graph(conn, unique_org, "proj", "building-validation")

    r1 = _check_upsert(g, insight="login e2e must pass", source="prd-proj", scope="validation",
                       meta={"check_id": "login-e2e", "run": "playwright test login"})
    r2 = _check_upsert(g, insight="login e2e must pass against the DEPLOYED service",
                       source="prd-proj", scope="validation",
                       meta={"check_id": "login-e2e", "run": "playwright test login --deployed"})

    assert r1["action"] == "added"
    assert r2["action"] == "updated"
    assert r2["id"] == r1["id"]                       # same fact, no duplicate
    assert len(g.facts_by(category="check", state=None)) == 1
    assert _run_of(g, "login-e2e") == "playwright test login --deployed"  # new run wins


def test_check_upsert_never_merges_or_surfaces(unique_org):
    """A check write never returns action='merged'/'surfaced' and never surfaces a
    contradiction — the reconciliation pipeline is bypassed entirely for checks."""
    from knowledge.serve.app import _check_upsert

    conn = db.connect()
    g = _check_graph(conn, unique_org, "proj", "building-validation")
    for i in range(3):
        r = _check_upsert(g, insight="a check about the same topic", source="prd-proj",
                          scope="validation", meta={"check_id": f"c{i}", "run": f"cmd{i}"})
        assert r["action"] in ("added", "updated")
        assert r["contradictionsSurfaced"] == 0
    assert len(g.facts_by(category="check", state=None)) == 3


@pytest.fixture
def unique_org(request):
    return "test_" + request.node.name
