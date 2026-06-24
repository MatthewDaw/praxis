"""HTTP-level API-key auth tests over the real app (Postgres, no model needed).

These exercise the ``make_current_user`` + ``active_org`` wiring through the
FastAPI app: a request with ``X-Praxis-Key`` authenticates and is org-scoped to
the key's org, a bad/revoked key is 401, and a key used against a different org
is 403. They hit ``GET /candidates`` (a read that needs no embedder), so they
require only a Postgres DSN — not OPENROUTER_API_KEY.

Auth is NOT disabled here (we unset the conftest seam) so the API-key path is
actually exercised rather than short-circuited to the dev principal.
"""

from __future__ import annotations

import os

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


@pytest.fixture
def env_auth_enabled(monkeypatch):
    # The API-key path only runs when the dev seam is off.
    monkeypatch.delenv("PRAXIS_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "client")


@pytest.fixture
def ctx(unique_org, env_auth_enabled):
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    conn.execute("DELETE FROM api_keys WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", "owner-sub")
    key_id, raw_key = apikeys.mint_key(conn, org, label="test")
    app = create_app(conn)
    client = TestClient(app)
    yield {"client": client, "org": org, "key_id": key_id, "raw_key": raw_key, "conn": conn}
    conn.execute("DELETE FROM api_keys WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    conn.close()


def test_api_key_authenticates_and_is_org_scoped(ctx):
    res = ctx["client"].get(
        "/candidates",
        headers={"X-Praxis-Key": ctx["raw_key"], "X-Praxis-Org": ctx["org"]},
    )
    assert res.status_code == 200, res.text
    assert isinstance(res.json(), list)


def test_bad_api_key_is_401(ctx):
    res = ctx["client"].get(
        "/candidates",
        headers={"X-Praxis-Key": "pxk_bogus", "X-Praxis-Org": ctx["org"]},
    )
    assert res.status_code == 401


def test_revoked_api_key_is_401(ctx):
    apikeys.revoke_key(ctx["conn"], ctx["key_id"])
    res = ctx["client"].get(
        "/candidates",
        headers={"X-Praxis-Key": ctx["raw_key"], "X-Praxis-Org": ctx["org"]},
    )
    assert res.status_code == 401


def test_api_key_org_mismatch_is_403(ctx):
    res = ctx["client"].get(
        "/candidates",
        headers={"X-Praxis-Key": ctx["raw_key"], "X-Praxis-Org": "some-other-org"},
    )
    assert res.status_code == 403
