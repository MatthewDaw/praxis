"""Integration tests for the candidate API server over the facts spine.

The server is Postgres-only and writes through the facade's REAL embedder
(``create_app`` injects no fake), so these tests require both a Postgres DSN and
an OPENROUTER_API_KEY — POST /insights and POST /candidates embed for real.

Auth is bypassed via conftest (PRAXIS_AUTH_DISABLED=1 -> principal sub="dev-user").
``active_org`` always checks org membership, so each test gets a unique throwaway
org with "dev-user" added as a member and sends it via the X-Praxis-Org header.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

# Load the repo-root .env so PRAXIS_DB_URL / OPENROUTER_API_KEY resolve at
# import time (the module-level skipif below reads them).
load_dotenv()

from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None or not os.getenv("OPENROUTER_API_KEY"),
    reason=(
        "needs a Postgres DSN (PRAXIS_DB_URL / AWS secret) AND OPENROUTER_API_KEY — "
        "the HTTP write path embeds candidates/insights via the real embedder"
    ),
)

USER = "dev-user"


@pytest.fixture
def client(unique_org):
    """A TestClient over a fresh throwaway tenant (org + dev-user membership)."""
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    # Start clean so create + membership + facts begin fresh and reruns isolate.
    conn.execute("DELETE FROM fact_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM cached_facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", USER)
    app = create_app(conn)
    yield TestClient(app, headers={"X-Praxis-Org": org})
    conn.execute("DELETE FROM fact_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM cached_facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    conn.close()


def _create(client, title="A lesson", content="Use typed payloads at API boundaries."):
    res = client.post("/candidates", json={"title": title, "content": content})
    assert res.status_code == 200, res.text
    return res.json()


def test_health_reports_postgres(client):
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["store"] == "postgres"


def test_create_list_get_candidate(client):
    created = _create(client)
    cid = created["id"]
    assert created["state"] == "proposed"

    listed = client.get("/candidates").json()
    assert any(c["id"] == cid for c in listed)

    got = client.get(f"/candidates/{cid}")
    assert got.status_code == 200
    assert got.json()["title"] == "A lesson"


def test_promote_advances_proposed_to_active(client):
    cid = _create(client)["id"]
    res = client.post(f"/candidates/{cid}/promote", json={})
    assert res.status_code == 200
    assert res.json()["state"] == "active"


def test_reject_decays(client):
    cid = _create(client)["id"]
    res = client.post(f"/candidates/{cid}/reject", json={"reason": "noise"})
    assert res.status_code == 200
    assert res.json()["state"] == "decayed"


def test_patch_updates_candidate(client):
    cid = _create(client, title="New lesson")["id"]
    res = client.patch(f"/candidates/{cid}", json={"title": "New lesson (edited)"})
    assert res.status_code == 200
    assert res.json()["title"] == "New lesson (edited)"


def test_delete_then_get_is_404(client):
    cid = _create(client)["id"]
    assert client.delete(f"/candidates/{cid}").status_code == 200
    assert client.get(f"/candidates/{cid}").status_code == 404


def test_promote_unknown_is_404(client):
    assert client.post("/candidates/nope/promote", json={}).status_code == 404


def test_graph_reflects_active_facts(client):
    cid = _create(client, content="Prefer dependency injection at boundaries.")["id"]
    # Proposed facts are not in the active graph.
    before = client.get("/graph").json()["graph"]
    assert not any(n["id"] == cid for n in before["nodes"])
    # Promote -> it becomes an active node.
    client.post(f"/candidates/{cid}/promote", json={})
    after = client.get("/graph").json()["graph"]
    assert any(n["id"] == cid for n in after["nodes"])
    assert "edges" in after


def test_snapshots_save_list_load_delete_round_trip(client):
    cid = _create(client, content="A fact worth snapshotting in the graph.")["id"]
    client.post(f"/candidates/{cid}/promote", json={})

    saved = client.post("/snapshots", json={"name": "snap1"})
    assert saved.status_code == 200
    assert saved.json()["count"] >= 1

    listed = client.get("/snapshots").json()["snapshots"]
    assert any(s["name"] == "snap1" for s in listed)

    loaded = client.post("/snapshots/snap1/load")
    assert loaded.status_code == 200
    assert loaded.json()["loaded"] >= 1

    deleted = client.delete("/snapshots/snap1")
    assert deleted.status_code == 200
    assert not any(s["name"] == "snap1" for s in client.get("/snapshots").json()["snapshots"])


def test_load_unknown_snapshot_is_404(client):
    assert client.post("/snapshots/nope/load").status_code == 404


def test_evals_cached_shape(client):
    body = client.get("/evals/cached").json()
    assert "cached" in body
    assert isinstance(body["cached"], list)


def test_insight_then_context_round_trips(client):
    res = client.post("/insights", json={"insight": "use uv, not pip, in this repo"})
    assert res.status_code == 200
    assert res.json()["action"] in {"added", "merged", "overwrote"}

    ctx = client.get("/context", params={"query": "how do I install deps?"}).json()
    assert "uv" in ctx["context"]
