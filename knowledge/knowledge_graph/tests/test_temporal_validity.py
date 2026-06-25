"""Integration test for bi-temporal validity (Graphiti/Zep model).

A fact carries a world-time validity window: ``valid_at`` (when it became true,
defaulting to insert time) and ``invalid_at`` (when it stopped being true,
``NULL`` while currently valid). When a fact loses a contradiction it is kept
(text intact, ``state`` rejected) *and* its window is closed at the winner's
``valid_at`` — so default recall drops it but a point-in-time ``as_of`` query
before the supersession still recovers it.

Gated like ``knowledge/serve/tests/test_org_sharing.py``: needs a real Postgres
DSN and OPENROUTER_API_KEY, and writes through the REAL embedder + default write
policy (real ConflictJudge), the same path the backend uses.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv

load_dotenv()

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (  # noqa: E402
    PostgresVectorGraph,
    default_write_policy,
)
from knowledge.serve import db  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None or not os.getenv("OPENROUTER_API_KEY"),
    reason=(
        "needs a Postgres DSN (PRAXIS_DB_URL / AWS secret) AND OPENROUTER_API_KEY — "
        "writes embed and judge contradictions via the real embedder/LLM"
    ),
)


def _graph(conn, org, user) -> PostgresVectorGraph:
    # Real embedder + default policy (redact, dedup, extract claims, detect
    # conflicts) — the production wiring. Fresh tenant per run. bootstrap() applies
    # the yoyo migrations idempotently so the valid_at/invalid_at columns exist.
    db.bootstrap()
    conn.execute("DELETE FROM facts WHERE org_id = %s AND user_id = %s", (org, user))
    return PostgresVectorGraph(conn, org, user, policy=default_write_policy())


def _row(conn, org, user, fact_id):
    return conn.execute(
        "SELECT state, valid_at, invalid_at FROM facts "
        "WHERE org_id = %s AND user_id = %s AND id = %s",
        (org, user, fact_id),
    ).fetchone()


def test_supersession_closes_validity_window_and_point_in_time_recall(unique_org):
    conn = db.connect()
    org, user = unique_org, "u1"
    graph = _graph(conn, org, user)

    # Fact A becomes the live truth.
    a_id = graph.write("the deploy timeout is 30 seconds", state="active")
    assert a_id is not None
    a_before = _row(conn, org, user, a_id)
    assert a_before is not None
    assert a_before[0] == "active"
    assert a_before[1] is not None  # valid_at defaulted to insert time
    assert a_before[2] is None      # invalid_at NULL == currently valid

    # A is recalled by default while currently valid.
    hits = graph.search("how long before deploy times out?", top_k=5, state=None)
    assert a_id in {h.fact.id for h in hits}

    # A marker between A's and B's validity: A is still valid here, B is not yet.
    between = datetime.now(timezone.utc)

    # Fact B contradicts and supersedes A (drives the non-destructive overwrite).
    from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision

    new_text = "the deploy timeout is 90 seconds"
    decision = WriteDecision(text=new_text, state="active")
    decision.embedding = graph._embed(new_text)
    decision.update_target_id = a_id
    b_id = graph._overwrite(decision)
    assert b_id != a_id

    # A is kept (text intact) but rejected AND its validity window is now closed.
    a_after = _row(conn, org, user, a_id)
    assert a_after is not None
    assert a_after[0] == "rejected"
    assert a_after[2] is not None  # invalid_at set
    b_row = _row(conn, org, user, b_id)
    assert b_row is not None and b_row[2] is None  # B currently valid
    # A's window closed at (or after) B's valid_at — the supersession instant.
    assert a_after[2] >= b_row[1] - timedelta(seconds=1)

    # Default search excludes the now-invalid A; B is the live answer.
    ids_now = {h.fact.id for h in graph.search("deploy timeout?", top_k=5, state=None)}
    assert a_id not in ids_now
    assert b_id in ids_now

    # Point-in-time recall: as of `between` (before B's validity), A is back.
    ids_then = {
        h.fact.id
        for h in graph.search("deploy timeout?", top_k=5, state=None, as_of=between)
    }
    assert a_id in ids_then
    assert b_id not in ids_then  # B not yet valid at `between`

    # And as of now, the as_of query agrees with the default view.
    ids_asof_now = {
        h.fact.id
        for h in graph.search(
            "deploy timeout?", top_k=5, state=None, as_of=datetime.now(timezone.utc)
        )
    }
    assert a_id not in ids_asof_now
    assert b_id in ids_asof_now


@pytest.fixture
def unique_org(request):
    return "test_" + request.node.name
