"""Integration tests for mounted read-only snapshot overlays.

A *mount* exposes a saved snapshot's facts to retrieval reads without merging
them into the live graph and without them being carried over when a snapshot is
saved. These tests prove that read-time union and, crucially, the two invariants
the feature promises: mounting never changes the live graph, and a saved snapshot
never includes mounted overlay facts.

Like test_server.py / test_org_sharing.py the server uses the REAL embedder
(create_app injects no fakes), so /context + /insights embed for real — needs a
Postgres DSN and OPENROUTER_API_KEY. Auth is bypassed via conftest
(PRAXIS_AUTH_DISABLED=1 -> principal sub="dev-user").
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.mounted_store import MountedStore  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None or not os.getenv("OPENROUTER_API_KEY"),
    reason="needs a Postgres DSN AND OPENROUTER_API_KEY (mount reads embed for real)",
)

USER = "dev-user"
USER_B = "userB"


def _wipe(conn, org):
    conn.execute("DELETE FROM mounted_snapshots WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM snapshot_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM snapshot_claims WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM snapshots WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM fact_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM spaces WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))


@pytest.fixture
def ctx(unique_org):
    """(client, conn, org) over a fresh org with dev-user (owner) + userB."""
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    _wipe(conn, org)
    store = OrgsStore(conn)
    store.create_org(org, org, "pw", USER)
    store.join_org(org, "pw", USER_B)
    app = create_app(conn)
    client = TestClient(app, headers={"X-Praxis-Org": org})
    yield client, conn, org
    _wipe(conn, org)
    conn.close()


def _count_cached(conn, org, user, name):
    """Count snapshot facts in the org-shared ``snapshots`` table (space-aware storage)."""
    row = conn.execute(
        "SELECT count(*) FROM snapshots WHERE org_id=%s AND space=%s AND snapshot=%s",
        (org, SPACE, name),
    ).fetchone()
    return int(row[0])


SPACE = "default"

# --- MountedStore CRUD (no embedding) --------------------------------------
def test_mounted_store_crud(ctx):
    _, conn, org = ctx
    store = MountedStore(conn)
    assert store.list(org, USER) == []
    store.mount(org, USER, SPACE, "wip")
    store.mount(org, USER, SPACE, "wip")  # idempotent
    assert store.list(org, USER) == [{"space": SPACE, "snapshot": "wip"}]
    store.unmount(org, USER, SPACE, "wip")
    assert store.list(org, USER) == []


# --- route validation (no embedding) ---------------------------------------
def test_mount_unknown_snapshot_404(ctx):
    client, _, _ = ctx
    r = client.post("/mounts", json={"space": SPACE, "snapshot": "does-not-exist"})
    assert r.status_code == 404


def test_mount_non_member_404(ctx):
    client, _, _ = ctx
    r = client.post("/mounts", json={"space": SPACE, "snapshot": "wip"})
    # Mounting your own snapshot that doesn't exist -> 404 (not found, not 400)
    assert r.status_code == 404


# --- the core read overlay + invariants (embeds) ---------------------------
def test_mounted_snapshot_is_recalled_but_not_merged_or_saved(ctx):
    client, conn, org = ctx

    # 1. Add a fact, snapshot it as "A", then clear the live graph.
    assert client.post("/insights", json={"insight": "We deploy on Fridays only."}).status_code == 200
    assert client.post("/snapshots", json={"space": SPACE, "snapshot": "A"}).status_code == 200
    a_count = _count_cached(conn, org, USER, "A")
    assert a_count >= 1
    assert client.post("/graph/clear").status_code == 200
    # Live graph is now empty.
    assert client.get("/candidates").json() == []

    # 2. Before mounting, /context recalls nothing.
    pre = client.get("/context", params={"query": "when do we deploy?"}).json()
    assert pre["hits"] == []

    # 3. Mount A — now /context recalls its facts, flagged mounted/snapshot.
    assert client.post("/mounts", json={"space": SPACE, "snapshot": "A"}).status_code == 200
    post = client.get("/context", params={"query": "when do we deploy?"}).json()
    assert post["hits"], "mounted snapshot should be recalled"
    assert all(h["mounted"] for h in post["hits"])
    assert all(h["snapshot"] == "A" for h in post["hits"])
    assert "Friday" in post["context"]

    # 4. Invariant: mounting did NOT merge into the live graph.
    assert client.get("/candidates").json() == []

    # 5. Invariant: a NEW save does NOT carry the mounted overlay.
    assert client.post("/insights", json={"insight": "Code review is required before merge."}).status_code == 200
    assert client.post("/snapshots", json={"space": SPACE, "snapshot": "B"}).status_code == 200
    b_count = _count_cached(conn, org, USER, "B")
    # B holds only the single live fact, not A's mounted facts.
    assert b_count == 1
    assert b_count < a_count + 1

    # 6. Unmount removes the overlay from reads.
    assert client.request("DELETE", "/mounts", json={"space": SPACE, "snapshot": "A"}).status_code == 200
    after = client.get("/context", params={"query": "when do we deploy?"}).json()
    assert all(not h["mounted"] for h in after["hits"])
