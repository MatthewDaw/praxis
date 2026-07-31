"""A snapshot-resident fact can be rejected, restored, and edited through the candidate surface.

``PATCH /candidates/{cid}`` already honours the ``(space, snapshot)`` target dependency, but
``POST /candidates/{cid}/promote`` and ``POST /candidates/{cid}/reject`` did NOT: they called
``candidates_for(org, uid)`` with no target, so every request resolved against the requester's
private working memory and a snapshot-resident fact simply 404'd. The consequence in production is
that a fact rejected inside a ``prd-<project>`` snapshot has NO PATH BACK — nothing can un-reject
it, which is how the ticket-merge corruption became unrecoverable rather than merely wrong.

This module pins the full round trip against a snapshot: reject -> promote (restore) -> edit, each
addressed with the ``X-Praxis-Space`` / ``X-Praxis-Snapshot`` pair, plus the negative that the same
call WITHOUT the pair still resolves against working memory (so the target is doing the work, not a
silent global lookup that would break tenancy).

The PATCH leg additionally needs the plan's planning marker ARMED: the S12 bless-state guard
refuses an edit against a blessed ``prd-<project>`` snapshot until planning is re-armed (see
``test_bless_guard``). The fixture arms it, so this test isolates the target-dependency behaviour
rather than re-testing the guard.

Offline apart from Postgres: the fixture swaps the default embedder for ``FakeEmbedder`` and only
the candidate surface is touched (no semantic write pipeline, no OPENROUTER key).
"""

from __future__ import annotations

import time

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"
PROJECT = "roundtripproj"
SNAPSHOT = f"prd-{PROJECT}"
HEADERS = {"X-Praxis-Space": PROJECT, "X-Praxis-Snapshot": SNAPSHOT}
TICKET_TEXT = "the exporter writes one csv row per scraped product"


@pytest.fixture
def env(unique_org, monkeypatch):
    monkeypatch.setenv("PRAXIS_AUTH_DISABLED", "1")
    from fastapi.testclient import TestClient

    from knowledge.knowledge_graph.knowledge_graph_variants import postgres_vector_graph
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.write_step_variants import Redactor
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder
    from knowledge.serve.app import create_app
    from knowledge.serve.orgs_store import OrgsStore
    from knowledge.serve.spaces_store import SpacesStore

    monkeypatch.setattr(postgres_vector_graph, "OpenRouterEmbedder", FakeEmbedder)

    org = unique_org
    tables = ("fact_edges", "facts", "snapshot_edges", "snapshots",
              "org_members", "orgs", "spaces")

    db.bootstrap()
    conn = db.connect()

    def _clean() -> None:
        for table in tables:
            conn.execute(f"DELETE FROM {table} WHERE org_id = %s", (org,))

    _clean()
    OrgsStore(conn).create_org(org, org, "pw", USER)
    SpacesStore(conn).create_space(org, PROJECT, PROJECT)

    graph = PostgresVectorGraph(
        conn, org, facts_table="snapshots", space=PROJECT, snapshot=SNAPSHOT,
        embedder=FakeEmbedder(), recall_floor=-1.0, policy=[Redactor()],
    )
    fid = graph.write(
        TICKET_TEXT, state="active", source=SNAPSHOT, category="requirement",
        meta={"title": "CSV export", "requirement_id": "R9", "build_state": "incomplete"},
    )
    # Arm the planning marker so the S12 bless guard permits the PATCH leg.
    marker_id = graph.ensure_planning_marker(PROJECT)
    graph.set_meta(marker_id, {"planning_owner": "sess-test", "planning_at": time.time()})

    client = TestClient(create_app(conn), headers={"X-Praxis-Org": org})
    yield client, conn, org, graph, fid
    _clean()
    conn.close()


def _state(graph, fid):
    fact = graph.get_fact(fid)
    return None if fact is None else fact.state


def test_reject_promote_edit_round_trip_against_a_snapshot_fact(env):
    """AC6 — the whole loop against a SNAPSHOT-resident fact, each call addressed with the pair.

    reject -> the fact is ``rejected``; promote -> it is restored to ``active`` (the path prod has
    none of); PATCH -> its content is edited. All three previously 404'd for promote/reject because
    the target was dropped.
    """
    client, conn, org, graph, fid = env

    rejected = client.post(f"/candidates/{fid}/reject", json={"reason": "written by mistake"},
                           headers=HEADERS)
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["id"] == fid
    assert _state(graph, fid) == "rejected"

    restored = client.post(f"/candidates/{fid}/promote", json={"targetState": "active"},
                           headers=HEADERS)
    assert restored.status_code == 200, restored.text
    assert restored.json()["id"] == fid
    assert _state(graph, fid) == "active", "a rejected snapshot fact must be restorable"

    edited = client.patch(f"/candidates/{fid}",
                          json={"content": TICKET_TEXT + " including the header row"},
                          headers=HEADERS)
    assert edited.status_code == 200, edited.text
    assert graph.get_fact(fid).text == TICKET_TEXT + " including the header row"
    assert _state(graph, fid) == "active"


def test_rejected_snapshot_fact_is_restorable_after_a_bare_promote_is_refused(env):
    """AC6 (negative + positive) — the ``(space, snapshot)`` pair is what makes it resolvable.

    The identical promote WITHOUT the headers resolves against working memory and 404s (the fact
    is not there — that is correct tenancy, not the bug), while the same call WITH the pair
    restores it. Pinning both halves keeps a future "look everywhere" shortcut from passing.
    """
    client, conn, org, graph, fid = env
    assert client.post(f"/candidates/{fid}/reject", json={}, headers=HEADERS).status_code == 200
    assert _state(graph, fid) == "rejected"

    bare = client.post(f"/candidates/{fid}/promote", json={})
    assert bare.status_code == 404, bare.text
    assert _state(graph, fid) == "rejected"  # and it changed nothing on the way out

    targeted = client.post(f"/candidates/{fid}/promote", json={}, headers=HEADERS)
    assert targeted.status_code == 200, targeted.text
    assert _state(graph, fid) == "active"


def test_round_trip_leaves_the_fact_in_the_snapshot_not_working_memory(env):
    """AC6 (durability) — the mutations act on the snapshot row itself; working memory stays empty,
    so nothing was silently copied into a private graph on the way through."""
    client, conn, org, graph, fid = env

    assert client.post(f"/candidates/{fid}/reject", json={}, headers=HEADERS).status_code == 200
    assert client.post(f"/candidates/{fid}/promote", json={}, headers=HEADERS).status_code == 200

    snapshot_rows = conn.execute(
        "SELECT id, state FROM snapshots WHERE org_id = %s AND space = %s AND snapshot = %s "
        "AND id = %s",
        (org, PROJECT, SNAPSHOT, fid),
    ).fetchall()
    assert snapshot_rows == [(fid, "active")]
    working = conn.execute(
        "SELECT count(*) FROM facts WHERE org_id = %s AND user_id = %s", (org, USER)
    ).fetchone()
    assert working[0] == 0


def test_reject_records_the_reason_on_the_snapshot_fact(env):
    """AC6 (audit) — a targeted reject is a real, attributable mutation of the snapshot fact, not
    a 200 over a no-op: the reason lands in the fact's own audit trail."""
    client, conn, org, graph, fid = env

    res = client.post(f"/candidates/{fid}/reject", json={"reason": "superseded by R12"},
                      headers=HEADERS)
    assert res.status_code == 200, res.text

    trail = (graph.get_fact(fid).meta or {}).get("auditTrail", [])
    rejects = [e for e in trail if e.get("action") == "rejected"]
    assert rejects, f"expected a 'rejected' audit entry, got {trail}"
    assert rejects[-1].get("note") == "superseded by R12"
