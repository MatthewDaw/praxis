"""Serve-level specs for the multi-agent build-loop ticket lease/claim.

Covers the four routes the agent factory drives to claim "tickets" (requirement
facts in ``prd-<project>``) without two parallel agents picking up the same one:
  * ``POST /requirements/{cid}/claim``     — atomically lease a ticket to an owner,
  * ``POST /requirements/{cid}/heartbeat`` — renew a live lease,
  * ``POST /requirements/{cid}/release``   — clear a lease + record build_state, and
  * ``GET  /requirements/incomplete?exclude_leased=true`` — hide live-leased tickets.

A claim is a LEASE, not a lock: it carries an owner + heartbeat, and a STALE lease
(dead/stalled agent) auto-reclaims so nothing dangles. All lease state lives on the
requirement fact's ``meta`` (no new table). Like the sibling read-surface tests we
seed via the graph object directly (no embedder key needed — the lease routes only
touch ``meta``, never embed).
"""

from __future__ import annotations

import threading
import time

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (  # noqa: E402
    LeaseConflict,
    PostgresVectorGraph,
)
from knowledge.knowledge_graph.write_policy.write_step_variants import (  # noqa: E402
    Deduper,
    Redactor,
)
from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder  # noqa: E402
from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"
PROJECT = "team-app"
SOURCE = f"prd-{PROJECT}"


@pytest.fixture
def env(unique_org):
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    tables = ("fact_edges", "facts", "snapshot_edges", "snapshots", "org_members", "orgs")
    for tbl in tables:
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", USER)
    app = create_app(conn)
    client = TestClient(app, headers={"X-Praxis-Org": org})
    graph = PostgresVectorGraph(
        conn, org, USER, embedder=FakeEmbedder(), recall_floor=-1.0,
        policy=[Redactor(), Deduper()],
    )
    yield client, graph, org
    for tbl in tables:
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    conn.close()


def _req(graph, text="The home screen lists today's tasks.", **extra):
    """Seed one active requirement ticket scoped to ``prd-team-app``."""
    return graph.write(
        text, state="active", category="requirement", source=SOURCE, **extra
    )


def _incomplete(client, **params):
    res = client.get("/requirements/incomplete", params={"project": PROJECT, **params})
    assert res.status_code == 200, res.text
    return {i["id"]: i for i in res.json()["incomplete"]}


# --- claim grant -----------------------------------------------------------
def test_claim_grants_and_marks_in_progress(env):
    client, graph, _ = env
    rid = _req(graph)
    res = client.post(f"/requirements/{rid}/claim", json={"owner": "agent-a"})
    assert res.status_code == 200, res.text
    claim = res.json()["claim"]
    assert claim["build_state"] == "in_progress"
    assert claim["claim_owner"] == "agent-a"
    assert claim["lease_live"] is True


def test_claim_by_second_owner_conflicts_409(env):
    client, graph, _ = env
    rid = _req(graph)
    assert client.post(f"/requirements/{rid}/claim", json={"owner": "agent-a"}).status_code == 200
    res = client.post(
        f"/requirements/{rid}/claim",
        json={"owner": "agent-b", "lease_ttl_seconds": 1800},
    )
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["owner"] == "agent-a"
    assert detail["remainingSeconds"] > 0  # the live holder still has time left


def test_same_owner_reclaim_is_idempotent(env):
    client, graph, _ = env
    rid = _req(graph)
    assert client.post(f"/requirements/{rid}/claim", json={"owner": "agent-a"}).status_code == 200
    # Re-claiming as the same owner renews rather than conflicting.
    res = client.post(f"/requirements/{rid}/claim", json={"owner": "agent-a"})
    assert res.status_code == 200, res.text
    assert res.json()["claim"]["claim_owner"] == "agent-a"


def test_claim_requires_owner_and_validates_ttl(env):
    client, graph, _ = env
    rid = _req(graph)
    assert client.post(f"/requirements/{rid}/claim", json={}).status_code == 400
    assert (
        client.post(
            f"/requirements/{rid}/claim",
            json={"owner": "a", "lease_ttl_seconds": "soon"},
        ).status_code
        == 400
    )


def test_claim_unknown_requirement_404(env):
    client, _, _ = env
    res = client.post("/requirements/does-not-exist/claim", json={"owner": "a"})
    assert res.status_code == 404


# --- heartbeat -------------------------------------------------------------
def test_heartbeat_renews_then_409_after_lease_lost(env):
    client, graph, _ = env
    rid = _req(graph)
    client.post(f"/requirements/{rid}/claim", json={"owner": "agent-a"})

    ok = client.post(f"/requirements/{rid}/heartbeat", json={"owner": "agent-a"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["claim"]["lease_live"] is True

    # A non-holder heartbeating is told it lost the lease.
    lost = client.post(f"/requirements/{rid}/heartbeat", json={"owner": "agent-b"})
    assert lost.status_code == 409, lost.text


# --- release ---------------------------------------------------------------
def test_release_finished_clears_lease_and_preserves_other_meta(env):
    client, graph, _ = env
    # Seed with sibling meta keys the release MUST NOT clobber.
    rid = _req(graph, meta={"requirement_id": "R1", "tags": ["auth"]})
    client.post(f"/requirements/{rid}/claim", json={"owner": "agent-a"})

    res = client.post(
        f"/requirements/{rid}/release", json={"owner": "agent-a", "state": "finished"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["claim"]["build_state"] == "finished"
    assert res.json()["claim"]["lease_live"] is False

    meta = graph.get_fact(rid).meta
    assert meta["build_state"] == "finished"
    assert meta["requirement_id"] == "R1"      # untouched
    assert meta["tags"] == ["auth"]            # untouched
    assert "claim_owner" not in meta           # lease keys dropped
    assert "claim_lease_ttl" not in meta


def test_release_rejects_bad_state_and_non_owner(env):
    client, graph, _ = env
    rid = _req(graph)
    client.post(f"/requirements/{rid}/claim", json={"owner": "agent-a"})
    assert (
        client.post(
            f"/requirements/{rid}/release", json={"owner": "agent-a", "state": "done"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/requirements/{rid}/release",
            json={"owner": "agent-b", "state": "finished"},
        ).status_code
        == 409
    )


def test_release_incomplete_makes_ticket_claimable_again(env):
    client, graph, _ = env
    rid = _req(graph)
    client.post(f"/requirements/{rid}/claim", json={"owner": "agent-a"})
    client.post(
        f"/requirements/{rid}/release", json={"owner": "agent-a", "state": "incomplete"}
    )
    # A different owner can now claim it (lease cleared).
    res = client.post(f"/requirements/{rid}/claim", json={"owner": "agent-b"})
    assert res.status_code == 200, res.text


# --- claim-aware incomplete listing ----------------------------------------
def test_incomplete_carries_claim_view_by_default(env):
    client, graph, _ = env
    rid = _req(graph)
    item = _incomplete(client)[rid]
    assert item["claim"]["build_state"] is None  # never claimed
    assert item["claim"]["lease_live"] is False


def test_exclude_leased_omits_live_keeps_unclaimed(env):
    client, graph, _ = env
    leased = _req(graph, "Leased ticket.")
    free = _req(graph, "Unclaimed ticket.")
    client.post(f"/requirements/{leased}/claim", json={"owner": "agent-a"})

    # Default: both present, each with a claim view.
    both = _incomplete(client)
    assert leased in both and free in both
    assert both[leased]["claim"]["lease_live"] is True

    # exclude_leased: the live-leased one drops, the unclaimed one stays.
    only_free = _incomplete(client, exclude_leased=True)
    assert leased not in only_free
    assert free in only_free


# --- lease expiry / auto-reclaim (the dead-agent path) ---------------------
def test_stale_lease_auto_reclaims_and_old_owner_heartbeat_409(env):
    client, graph, _ = env
    rid = _req(graph)
    # A 1s lease, then wait past it so the lease goes stale (agent "died").
    assert (
        client.post(
            f"/requirements/{rid}/claim",
            json={"owner": "agent-a", "lease_ttl_seconds": 1},
        ).status_code
        == 200
    )
    time.sleep(1.3)

    # A stale ticket is still claimable: it stays in the exclude_leased listing.
    assert rid in _incomplete(client, exclude_leased=True)

    # A different owner reclaims the dead lease.
    res = client.post(f"/requirements/{rid}/claim", json={"owner": "agent-b"})
    assert res.status_code == 200, res.text
    assert res.json()["claim"]["claim_owner"] == "agent-b"

    # The original owner's heartbeat now fails — it lost the ticket.
    lost = client.post(f"/requirements/{rid}/heartbeat", json={"owner": "agent-a"})
    assert lost.status_code == 409, lost.text


# --- atomicity (two concurrent claims for one ticket) ----------------------
def test_concurrent_claims_yield_exactly_one_grant(env):
    """Two agents race to claim the same ticket on independent connections; the
    atomic conditional UPDATE must grant exactly one (one 200, one LeaseConflict)."""
    _, graph, org = env
    rid = _req(graph)

    # Each racer gets its OWN connection (a psycopg connection is not concurrency
    # safe), pointed at the same tenant row.
    conns = [db.connect(), db.connect()]
    graphs = [PostgresVectorGraph(c, org, USER, embedder=FakeEmbedder()) for c in conns]
    results: list[object] = [None, None]
    barrier = threading.Barrier(2)

    def race(i: int, owner: str) -> None:
        barrier.wait()  # line both threads up on the UPDATE
        try:
            results[i] = graphs[i].claim_requirement(rid, owner)
        except LeaseConflict as exc:
            results[i] = exc

    threads = [
        threading.Thread(target=race, args=(0, "agent-a")),
        threading.Thread(target=race, args=(1, "agent-b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for c in conns:
        c.close()

    grants = [r for r in results if isinstance(r, dict)]
    conflicts = [r for r in results if isinstance(r, LeaseConflict)]
    assert len(grants) == 1, results
    assert len(conflicts) == 1, results
    assert grants[0]["build_state"] == "in_progress"
