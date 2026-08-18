"""``record_outcome`` must reconcile ``meta.build_state`` — the single source of truth.

THE INCIDENT (farming_analysis, 2026-08-09). An orphan-branch sweep wrongly regressed a ticket, and
the operator repaired it the documented way: ``praxis_record_outcome(fact_id, success=true)``. The
outcome landed — ``success_count`` went up, ``last_outcome`` read ``succeeded`` — and the build loop
went right on re-dispatching the ticket, because the loop's FIND reads ``meta.build_state`` and the
outcome columns live somewhere else entirely. Two stores of the same truth, one of them updated.

So an outcome recorded against a REQUIREMENT that carries ``meta.build_state`` now moves that state
with it, and clears the lease either way — a dangling ``claim_owner`` is precisely what stops the
next FIND from picking a ticket up.

It is done in ``PostgresVectorGraph.record_outcome``, not in the route, so the HTTP route, the MCP
tool and any internal caller all get the same reconciliation. These tests drive the GRAPH directly
for that reason, then check the route once to prove the wiring.
"""

from __future__ import annotations

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"
PROJECT = "proj"
SNAP = f"prd-{PROJECT}"

LEASE = {
    "claim_owner": "worker-a",
    "claim_at": 1_700_000_000.0,
    "claim_heartbeat_at": 1_700_000_100.0,
    "claim_lease_ttl": 900,
}


@pytest.fixture
def seeded(request, monkeypatch):
    monkeypatch.setenv("PRAXIS_AUTH_DISABLED", "1")
    from fastapi.testclient import TestClient

    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder
    from knowledge.serve.app import create_app
    from knowledge.serve.orgs_store import OrgsStore
    from knowledge.serve.spaces_store import SpacesStore

    org = "test_" + request.node.name
    db.bootstrap()
    conn = db.connect()

    def _clean():
        for t in ("fact_edges", "facts", "snapshots", "org_members", "orgs", "spaces"):
            try:
                conn.execute(f"DELETE FROM {t} WHERE org_id = %s", (org,))
            except Exception:
                pass

    _clean()
    OrgsStore(conn).create_org(org, org, "pw", USER)
    SpacesStore(conn).create_space(org, PROJECT, PROJECT)

    g = PostgresVectorGraph(
        conn, org, facts_table="snapshots", space=PROJECT, snapshot=SNAP,
        embedder=FakeEmbedder(), recall_floor=-1.0,
    )
    app = create_app()
    yield {
        "graph": g,
        "org": org,
        "client": TestClient(app, headers={"X-Praxis-Org": org}),
    }
    _clean()


def _ticket(graph, build_state: str, *, lease: bool = True, category: str = "requirement") -> str:
    meta: dict = {"requirement_id": "R27", "build_state": build_state}
    if lease:
        meta.update(LEASE)
    return graph.write(f"requirement: a thing ({build_state})", state="active", source=SNAP,
                       category=category, meta=meta)


def _meta(graph, fid: str) -> dict:
    fact = graph.get_fact(fid)
    return dict((fact.meta if fact else None) or {})


# --------------------------------------------------------------------------- success --

@pytest.mark.parametrize("start", ["incomplete", "in_progress", "blocked"])
def test_success_finishes_a_dispatched_ticket_and_clears_the_lease(seeded, start):
    """The repair path. Every state a ticket can be dispatched in must answer to a success."""
    g = seeded["graph"]
    fid = _ticket(g, start)

    g.record_outcome(fid, success=True)

    meta = _meta(g, fid)
    assert meta["build_state"] == "finished", (
        f"a success against a {start} ticket left it {meta['build_state']!r} — the loop's FIND reads "
        "this field, so the ticket goes on being re-dispatched forever"
    )
    for key in LEASE:
        assert key not in meta, f"{key} still held the ticket; a live lease blocks the next FIND"
    assert meta["requirement_id"] == "R27", "unrelated meta must survive the merge"
    assert meta.get("finished_at"), "a finished ticket is dated by the server"


def test_failure_returns_a_ticket_to_incomplete_and_clears_the_lease(seeded):
    g = seeded["graph"]
    fid = _ticket(g, "in_progress")

    g.record_outcome(fid, success=False)

    meta = _meta(g, fid)
    assert meta["build_state"] == "incomplete"
    assert not any(k in meta for k in LEASE)
    assert "finished_at" not in meta


def test_the_outcome_columns_are_still_recorded(seeded):
    """The reconciliation is an ADDITION. Trust weighting is what this method was for."""
    g = seeded["graph"]
    fid = _ticket(g, "in_progress")

    g.record_outcome(fid, success=True)

    row = g._conn.execute(
        "SELECT success_count, failure_count, last_outcome FROM snapshots WHERE id = %s", (fid,)
    ).fetchone()
    assert row[0] == 1 and row[1] == 0 and row[2] == "succeeded"


# --------------------------------------------------------------------------- what it must NOT touch --

def test_a_finished_ticket_is_not_regressed_by_a_failure(seeded):
    """Regressing is ``regress_requirements``' job: it carries the audit disposition and the cycle
    cap that a bare outcome cannot supply. A failure outcome must not become a silent side-door
    regress with no recorded reason."""
    g = seeded["graph"]
    fid = _ticket(g, "finished", lease=False)

    g.record_outcome(fid, success=False)

    assert _meta(g, fid)["build_state"] == "finished"


def test_a_requirement_with_no_build_state_is_left_alone(seeded):
    """A ticket that has never been dispatched has no dispatch to reconcile — inventing one would
    mark work finished that nobody ever did."""
    g = seeded["graph"]
    fid = g.write("requirement: never dispatched", state="active", source=SNAP,
                  category="requirement", meta={"requirement_id": "R99"})

    g.record_outcome(fid, success=True)

    assert "build_state" not in _meta(g, fid)


def test_a_plain_knowledge_fact_is_untouched(seeded):
    """``record_outcome`` is the general trust signal for EVERY fact. Only requirements have a build
    lifecycle, and a lesson that happens to carry a `build_state`-shaped key is not a ticket."""
    g = seeded["graph"]
    fid = _ticket(g, "in_progress", category="lesson")

    g.record_outcome(fid, success=True)

    meta = _meta(g, fid)
    assert meta["build_state"] == "in_progress"
    assert meta["claim_owner"] == "worker-a"


# --------------------------------------------------------------------------- wiring --

def test_the_http_route_gets_the_reconciliation_too(seeded):
    """The route delegates; it must not have to know. This is the exact call the MCP
    ``praxis_record_outcome`` tool makes, and the one the operator's repair went through."""
    g, client, org = seeded["graph"], seeded["client"], seeded["org"]
    fid = _ticket(g, "in_progress")

    resp = client.post(
        f"/facts/{fid}/outcome", json={"success": True},
        headers={"X-Praxis-Org": org, "X-Praxis-Space": PROJECT, "X-Praxis-Snapshot": SNAP},
    )

    assert resp.status_code == 200, resp.text
    assert _meta(g, fid)["build_state"] == "finished"
