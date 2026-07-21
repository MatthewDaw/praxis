"""Requirement TICKETS are identity-keyed on meta.requirement_id and NEVER text-deduped/reconciled.

Regression for the AMEND (C0) data-corruption footgun: ``praxis_add_insight(category="requirement",
on_conflict="surface")`` for a genuinely-NEW ticket that was merely TOPICALLY SIMILAR to an existing
one got silently MERGED by the normal dedup into the nearest fact — returning ``action="merged"`` and
APPENDING its text into a (sometimes already-``finished``) ticket's ``content``, corrupting it.
``on_conflict`` only governs CONTRADICTIONS, so ``"surface"`` never guarded that additive merge. A
ticket is a distinct BUILD UNIT keyed on ``requirement_id``, not a knowledge assertion to reconcile;
two tickets with different ids stay distinct even when their prose reads alike, and a NEW ticket must
never mutate a DIFFERENT (or ``finished``) ticket.

Exercises ``_requirement_upsert`` directly on a snapshot-bound, REDACT-ONLY graph — the exact graph
the ``/insights`` endpoint builds for a requirement-ticket write — so no HTTP client / OPENROUTER is
needed. Postgres-gated (skips without a DSN), same as ``test_check_upsert``.
"""

from __future__ import annotations

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)


def _req_graph(conn, org, space, snap):
    """A snapshot-bound, redact-only graph — what the /insights requirement-ticket path constructs."""
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


def _by_rid(graph, rid):
    for f in graph.facts_by(category="requirement", state=None, meta_filter={"requirement_id": rid}):
        return f
    return None


def test_new_ticket_similar_to_finished_lands_distinct_and_never_mutates_it(unique_org):
    """THE acceptance test: a NEW ticket (distinct requirement_id) topically similar to a FINISHED
    ticket lands as a DISTINCT new fact and NEVER touches the finished ticket's content/build_state."""
    from knowledge.serve.app import _requirement_upsert

    conn = db.connect()
    g = _req_graph(conn, unique_org, "proj", "prd-proj")

    finished = _requirement_upsert(
        g, insight="the source-discriminator dedups scraped rows by canonical url",
        source="prd-proj", scope="mvp",
        meta={"requirement_id": "R30", "build_state": "finished",
              "tags": ["source-discriminator"], "acceptance": "rows with same url collapse to one"},
    )
    assert finished["action"] == "added"
    finished_id = finished["id"]

    # A genuinely-NEW ticket: distinct requirement_id, build_state incomplete, DELIBERATELY similar prose.
    new = _requirement_upsert(
        g, insight="the source-discriminator must also dedup scraped rows by product id",
        source="prd-proj", scope="mvp",
        meta={"requirement_id": "R42", "build_state": "incomplete", "tags": ["source-discriminator"]},
    )

    # Distinct new fact — never merged, never surfaced.
    assert new["action"] == "added"
    assert new["id"] != finished_id
    assert new["contradictionsSurfaced"] == 0
    assert len(g.facts_by(category="requirement", state=None)) == 2

    # The FINISHED ticket is byte-for-byte untouched: content, build_state, and id all preserved.
    f = _by_rid(g, "R30")
    assert f.id == finished_id
    assert f.text == "the source-discriminator dedups scraped rows by canonical url"
    assert "product id" not in f.text                       # the new ticket's text did NOT append
    assert (f.meta or {}).get("build_state") == "finished"  # lifecycle state survived
    assert (f.meta or {}).get("requirement_id") == "R30"
    assert (f.meta or {}).get("acceptance") == "rows with same url collapse to one"


def test_same_requirement_id_updates_in_place(unique_org):
    """A TRUE restatement of the SAME ticket (same requirement_id) updates that one fact in place —
    no duplicate, no new id — so identity-keying still catches an actual re-file of the same ticket."""
    from knowledge.serve.app import _requirement_upsert

    conn = db.connect()
    g = _req_graph(conn, unique_org, "proj", "prd-proj")

    r1 = _requirement_upsert(
        g, insight="pagination stops at the last page", source="prd-proj", scope="mvp",
        meta={"requirement_id": "R7", "build_state": "incomplete"},
    )
    r2 = _requirement_upsert(
        g, insight="pagination stops at the last page AND never loops",
        source="prd-proj", scope="mvp",
        meta={"requirement_id": "R7", "build_state": "incomplete"},
    )

    assert r1["action"] == "added"
    assert r2["action"] == "updated"
    assert r2["id"] == r1["id"]                              # same fact, no duplicate
    assert len(g.facts_by(category="requirement", state=None)) == 1
    assert _by_rid(g, "R7").text == "pagination stops at the last page AND never loops"


def test_ticket_write_never_merges_or_surfaces(unique_org):
    """Every requirement-ticket write returns added/updated and never merged/surfaced — the
    reconciliation pipeline is bypassed entirely (mirrors the check-upsert guarantee)."""
    from knowledge.serve.app import _requirement_upsert

    conn = db.connect()
    g = _req_graph(conn, unique_org, "proj", "prd-proj")
    for i in range(3):
        r = _requirement_upsert(
            g, insight="a requirement about the very same topic", source="prd-proj", scope="mvp",
            meta={"requirement_id": f"R{i}", "build_state": "incomplete"},
        )
        assert r["action"] in ("added", "updated")
        assert r["contradictionsSurfaced"] == 0
    assert len(g.facts_by(category="requirement", state=None)) == 3


@pytest.fixture
def unique_org(request):
    return "test_" + request.node.name
