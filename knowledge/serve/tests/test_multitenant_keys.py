"""Two org-scoped keys must coexist and never disturb each other (blocker #3).

This is the multi-tenancy guarantee: a ``bestie``-scoped key and a ``sotos``-scoped
key are BOTH valid simultaneously, each authenticates only its own org, and using
one never affects the other.

Two layers:
  * offline — both keys resolve concurrently to distinct orgs via the auth
    dependency (no Postgres), proving isolation at the resolution layer;
  * end-to-end (Postgres-gated) — the full app enforces 200 for a key's own org,
    403 for a sibling org, and ``/whoami`` reports the right ``keyOrg``/``orgMatch``.
"""

from __future__ import annotations

import pytest
from dotenv import load_dotenv

from knowledge.serve import apikeys, auth

# ------------------------------------------------------------------ offline layer


class _Cursor:
    def __init__(self, rows, rowcount=1):
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _MultiKeyConn:
    """In-memory ``api_keys`` fake holding many keys for many orgs at once."""

    def __init__(self):
        self.keys: dict[str, dict] = {}

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO api_keys"):
            kid, key_hash, org_id, user_id, label = params
            self.keys[kid] = {"key_hash": key_hash, "org_id": org_id,
                              "user_id": user_id, "label": label, "revoked": False}
            return _Cursor([])
        if s.startswith("SELECT id, org_id, user_id FROM api_keys"):
            (key_hash,) = params
            for kid, r in self.keys.items():
                if r["key_hash"] == key_hash and not r["revoked"]:
                    return _Cursor([(kid, r["org_id"], r["user_id"])])
            return _Cursor([])
        if s.startswith("UPDATE api_keys SET last_used_at"):
            return _Cursor([])
        raise AssertionError(f"unexpected SQL: {s}")


def test_two_org_keys_resolve_concurrently_and_isolated(monkeypatch):
    monkeypatch.delenv("PRAXIS_AUTH_DISABLED", raising=False)
    conn = _MultiKeyConn()
    _, bestie_key = apikeys.mint_key(conn, "bestie", user_id="bestie-user")
    _, sotos_key = apikeys.mint_key(conn, "sotos", user_id="sotos-user")

    dep = auth.make_current_user(conn)

    # Both authenticate — concurrently, from the same store.
    bestie = dep(authorization=None, x_praxis_key=bestie_key)
    sotos = dep(authorization=None, x_praxis_key=sotos_key)

    assert bestie.api_key_org == "bestie" and bestie.sub == "bestie-user"
    assert sotos.api_key_org == "sotos" and sotos.sub == "sotos-user"
    # Neither key ever resolves to the other's org.
    assert apikeys.resolve_key(conn, bestie_key).org_id == "bestie"
    assert apikeys.resolve_key(conn, sotos_key).org_id == "sotos"


# --------------------------------------------------------- end-to-end (Postgres)

load_dotenv()
from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

pytestmark_e2e = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret)",
)


@pytest.fixture
def two_orgs(unique_org, monkeypatch):
    monkeypatch.delenv("PRAXIS_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "client")
    db.bootstrap()
    conn = db.connect()
    bestie, sotos = unique_org + "_bestie", unique_org + "_sotos"
    for org, owner in ((bestie, "bestie-owner"), (sotos, "sotos-owner")):
        conn.execute("DELETE FROM api_keys WHERE org_id = %s", (org,))
        conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
        conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
        OrgsStore(conn).create_org(org, org, "pw", owner)
    _, bestie_key = apikeys.mint_key(conn, bestie, label="bestie")
    _, sotos_key = apikeys.mint_key(conn, sotos, label="sotos")
    client = TestClient(create_app(conn))
    yield {"client": client, "bestie": bestie, "sotos": sotos,
           "bestie_key": bestie_key, "sotos_key": sotos_key}
    for org in (bestie, sotos):
        conn.execute("DELETE FROM api_keys WHERE org_id = %s", (org,))
        conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
        conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    conn.close()


@pytestmark_e2e
def test_each_key_owns_its_org_and_is_barred_from_the_sibling(two_orgs):
    c = two_orgs["client"]
    bestie, sotos = two_orgs["bestie"], two_orgs["sotos"]
    bk, sk = two_orgs["bestie_key"], two_orgs["sotos_key"]

    # Own org -> 200; sibling org -> 403. Both keys, both directions, concurrently valid.
    assert c.get("/candidates", headers={"X-Praxis-Key": bk, "X-Praxis-Org": bestie}).status_code == 200
    assert c.get("/candidates", headers={"X-Praxis-Key": bk, "X-Praxis-Org": sotos}).status_code == 403
    assert c.get("/candidates", headers={"X-Praxis-Key": sk, "X-Praxis-Org": sotos}).status_code == 200
    assert c.get("/candidates", headers={"X-Praxis-Key": sk, "X-Praxis-Org": bestie}).status_code == 403


@pytestmark_e2e
def test_whoami_reports_key_org_and_mismatch(two_orgs):
    c = two_orgs["client"]
    bestie, sotos = two_orgs["bestie"], two_orgs["sotos"]
    bk = two_orgs["bestie_key"]

    ok = c.get("/whoami", headers={"X-Praxis-Key": bk, "X-Praxis-Org": bestie}).json()
    assert ok["authMode"] == "key" and ok["keyOrg"] == bestie and ok["orgMatch"] is True

    bad = c.get("/whoami", headers={"X-Praxis-Key": bk, "X-Praxis-Org": sotos}).json()
    assert bad["authMode"] == "key" and bad["keyOrg"] == bestie and bad["orgMatch"] is False
    assert sotos in bad["detail"] and bestie in bad["detail"]
