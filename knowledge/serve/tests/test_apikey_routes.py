"""HTTP tests for the in-page API-key management routes.

These exercise POST/GET /apikeys and POST /apikeys/{id}/revoke through the real
FastAPI app. They need only a Postgres DSN (no embedder / OPENROUTER_API_KEY):
the routes touch only the ``api_keys`` table, never the facts pipeline.

Auth uses the conftest dev seam (PRAXIS_AUTH_DISABLED=1 -> principal
sub="dev-user"); ``active_org`` still checks org membership, so each test gets a
unique throwaway org with "dev-user" as a member, sent via X-Praxis-Org.
"""

from __future__ import annotations

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.serve import apikeys, db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret)",
)

USER = "dev-user"


def _seed_org(conn, org):
    conn.execute("DELETE FROM api_keys WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", USER)


@pytest.fixture
def ctx(unique_org):
    db.bootstrap()
    conn = db.connect()
    org_a = unique_org + "_a"
    org_b = unique_org + "_b"
    _seed_org(conn, org_a)
    _seed_org(conn, org_b)
    app = create_app(conn)
    client = TestClient(app)
    yield {"client": client, "conn": conn, "org_a": org_a, "org_b": org_b}
    for org in (org_a, org_b):
        conn.execute("DELETE FROM api_keys WHERE org_id = %s", (org,))
        conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
        conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    conn.close()


def test_create_list_revoke_lifecycle(ctx):
    client, org = ctx["client"], ctx["org_a"]
    headers = {"X-Praxis-Org": org}

    # create
    res = client.post("/apikeys", json={"label": "ci"}, headers=headers)
    assert res.status_code == 200, res.text
    created = res.json()
    assert created["key"].startswith("pxk_")
    assert created["label"] == "ci"
    assert created["id"]
    assert created["createdAt"]
    key_id = created["id"]

    # appears in list, no raw key / hash leaked, scoped to caller's user id
    res = client.get("/apikeys", headers=headers)
    assert res.status_code == 200, res.text
    listed = res.json()
    assert isinstance(listed, list)
    row = next(k for k in listed if k["id"] == key_id)
    assert set(row) == {"id", "label", "userId", "createdAt", "lastUsedAt", "revoked"}
    assert row["userId"] == USER
    assert row["label"] == "ci"
    assert row["revoked"] is False
    # no raw key or hash anywhere in the list payload
    assert "key" not in row and "keyHash" not in row and "key_hash" not in row
    assert created["key"] not in res.text

    # revoke
    res = client.post(f"/apikeys/{key_id}/revoke", headers=headers)
    assert res.status_code == 200, res.text
    assert res.json() == {"id": key_id, "revoked": True}

    # shows revoked
    res = client.get("/apikeys", headers=headers)
    row = next(k for k in res.json() if k["id"] == key_id)
    assert row["revoked"] is True


def test_null_label_create(ctx):
    client, org = ctx["client"], ctx["org_a"]
    res = client.post("/apikeys", json={"label": None}, headers={"X-Praxis-Org": org})
    assert res.status_code == 200, res.text
    assert res.json()["label"] is None


def test_cross_org_isolation(ctx):
    client, conn = ctx["client"], ctx["conn"]
    org_a, org_b = ctx["org_a"], ctx["org_b"]

    # a key belonging to org B
    key_id_b, _ = apikeys.mint_key(conn, org_b, user_id=USER, label="b-key")

    # org A's list must not contain org B's key
    res = client.get("/apikeys", headers={"X-Praxis-Org": org_a})
    assert res.status_code == 200, res.text
    assert all(k["id"] != key_id_b for k in res.json())

    # org A cannot revoke org B's key -> 404
    res = client.post(f"/apikeys/{key_id_b}/revoke", headers={"X-Praxis-Org": org_a})
    assert res.status_code == 404, res.text

    # and org B's key is still active (not revoked)
    assert all(
        not k["revoked"] for k in apikeys.list_keys(conn, org_b) if k["id"] == key_id_b
    )


def test_revoke_unknown_key_is_404(ctx):
    client, org = ctx["client"], ctx["org_a"]
    res = client.post("/apikeys/does-not-exist/revoke", headers={"X-Praxis-Org": org})
    assert res.status_code == 404
