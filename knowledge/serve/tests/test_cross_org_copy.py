"""Integration tests for cross-org sharing: copy a snapshot or a whole space
into another org the SAME login belongs to.

Two endpoints are under test:
  * POST /snapshots/{name}/copy-to-org  — copy a saved snapshot's cache into the
    caller's DEFAULT graph in ``targetOrg`` (ids/embeddings preserved verbatim).
  * POST /spaces/copy-to-org            — copy the caller's active working graph
    into a freshly created space in ``targetOrg``.

The copy is pure SQL (no embedder/LLM), so unlike the fold-in tests these only
need a Postgres DSN — facts are seeded directly. Auth is bypassed via conftest
(PRAXIS_AUTH_DISABLED=1 -> principal sub="dev-user"); ``active_org`` still checks
membership, so the test makes dev-user a member of BOTH orgs.
"""

from __future__ import annotations

import pytest
from dotenv import load_dotenv

from fastapi.testclient import TestClient

load_dotenv()

from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret); the copy path is pure SQL",
)

USER = "dev-user"  # the PRAXIS_AUTH_DISABLED dev principal sub


def _wipe(conn, org):
    conn.execute("DELETE FROM fact_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM cached_fact_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM cached_facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM spaces WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))


@pytest.fixture
def ctx(unique_org):
    """(client, conn, src_org, dst_org) with dev-user a member of both orgs.

    The client carries ``X-Praxis-Org: src_org`` by default (the copy SOURCE);
    individual requests override the header where a test needs the destination.
    """
    db.bootstrap()
    conn = db.connect()
    src_org = unique_org
    dst_org = unique_org + "_dst"
    third_org = unique_org + "_third"
    for org in (src_org, dst_org, third_org):
        _wipe(conn, org)
    store = OrgsStore(conn)
    store.create_org(src_org, src_org, "pw", USER)
    store.create_org(dst_org, dst_org, "pw", USER)
    # third_org exists but dev-user is NOT a member (a non-member target).
    store.create_org(third_org, third_org, "pw", "someone-else")
    app = create_app(conn)
    client = TestClient(app, headers={"X-Praxis-Org": src_org})
    yield client, conn, src_org, dst_org, third_org
    for org in (src_org, dst_org, third_org):
        _wipe(conn, org)
    conn.close()


def _seed_cached(conn, org, key, fid, text, *, user=USER, scope=None, state="active"):
    conn.execute(
        "INSERT INTO cached_facts (id, org_id, user_id, cache_key, text, scope, state) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (fid, org, user, key, text, scope, state),
    )


def _seed_cached_edge(conn, org, key, src, dst, *, user=USER, kind="related"):
    conn.execute(
        "INSERT INTO cached_fact_edges (org_id, user_id, cache_key, src_id, dst_id, kind) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (org, user, key, src, dst, kind),
    )


def _seed_live(conn, org, fid, text, *, user=USER, scope=None, state="active"):
    conn.execute(
        "INSERT INTO facts (id, org_id, user_id, text, scope, state) VALUES (%s,%s,%s,%s,%s,%s)",
        (fid, org, user, text, scope, state),
    )


def _cached_rows(conn, org, key, *, user=USER):
    return conn.execute(
        "SELECT id, text FROM cached_facts WHERE org_id=%s AND user_id=%s AND cache_key=%s",
        (org, user, key),
    ).fetchall()


def _live_rows(conn, org, *, user):
    return conn.execute(
        "SELECT id, text FROM facts WHERE org_id=%s AND user_id=%s", (org, user)
    ).fetchall()


# --- POST /snapshots/{name}/copy-to-org ------------------------------------
def test_copy_snapshot_to_org_preserves_ids_and_facts(ctx):
    client, conn, src_org, dst_org, _third = ctx
    _seed_cached(conn, src_org, "snapshot:snap", "f1", "always run the linter")
    _seed_cached(conn, src_org, "snapshot:snap", "f2", "prefer composition")
    _seed_cached_edge(conn, src_org, "snapshot:snap", "f1", "f2")

    res = client.post("/snapshots/snap/copy-to-org", json={"targetOrg": dst_org})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body == {"targetOrg": dst_org, "name": "snap", "count": 2}

    # Copied verbatim into dst org's default graph, same ids preserved.
    copied = {(r[0], r[1]) for r in _cached_rows(conn, dst_org, "snapshot:snap")}
    assert copied == {("f1", "always run the linter"), ("f2", "prefer composition")}
    edges = conn.execute(
        "SELECT src_id, dst_id, kind FROM cached_fact_edges "
        "WHERE org_id=%s AND user_id=%s AND cache_key=%s",
        (dst_org, USER, "snapshot:snap"),
    ).fetchall()
    assert [tuple(e) for e in edges] == [("f1", "f2", "related")]
    # Source untouched.
    assert len(_cached_rows(conn, src_org, "snapshot:snap")) == 2


def test_copy_snapshot_to_org_with_rename(ctx):
    client, conn, src_org, dst_org, _third = ctx
    _seed_cached(conn, src_org, "snapshot:snap", "f1", "a fact")
    res = client.post(
        "/snapshots/snap/copy-to-org",
        json={"targetOrg": dst_org, "targetName": "renamed"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "renamed"
    assert len(_cached_rows(conn, dst_org, "snapshot:renamed")) == 1
    assert len(_cached_rows(conn, dst_org, "snapshot:snap")) == 0


def test_copy_snapshot_unknown_source_is_404(ctx):
    client, _conn, _src, dst_org, _third = ctx
    res = client.post("/snapshots/ghost/copy-to-org", json={"targetOrg": dst_org})
    assert res.status_code == 404


def test_copy_snapshot_existing_target_name_is_409(ctx):
    client, conn, src_org, dst_org, _third = ctx
    _seed_cached(conn, src_org, "snapshot:snap", "f1", "source fact")
    _seed_cached(conn, dst_org, "snapshot:snap", "x1", "already here")
    res = client.post("/snapshots/snap/copy-to-org", json={"targetOrg": dst_org})
    assert res.status_code == 409
    # The pre-existing target snapshot was not overwritten.
    assert {r[1] for r in _cached_rows(conn, dst_org, "snapshot:snap")} == {"already here"}


def test_copy_snapshot_non_member_target_is_403(ctx):
    client, conn, src_org, _dst, third_org = ctx
    _seed_cached(conn, src_org, "snapshot:snap", "f1", "source fact")
    res = client.post("/snapshots/snap/copy-to-org", json={"targetOrg": third_org})
    assert res.status_code == 403


def test_copy_snapshot_missing_target_org_is_400(ctx):
    client, conn, src_org, _dst, _third = ctx
    _seed_cached(conn, src_org, "snapshot:snap", "f1", "source fact")
    assert client.post("/snapshots/snap/copy-to-org", json={}).status_code == 400


# --- POST /spaces/copy-to-org ----------------------------------------------
def test_copy_space_to_org_creates_space_and_copies_graph(ctx):
    client, conn, src_org, dst_org, _third = ctx
    _seed_live(conn, src_org, "f1", "space fact one")
    _seed_live(conn, src_org, "f2", "space fact two")
    conn.execute(
        "INSERT INTO fact_edges (org_id, user_id, src_id, dst_id, kind) VALUES (%s,%s,%s,%s,%s)",
        (src_org, USER, "f1", "f2", "related"),
    )

    res = client.post(
        "/spaces/copy-to-org", json={"targetOrg": dst_org, "targetSpace": "copied"}
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"targetOrg": dst_org, "space": "copied", "count": 2}

    # The space row was created in the destination org...
    owns = conn.execute(
        "SELECT 1 FROM spaces WHERE org_id=%s AND owner_sub=%s AND space_id=%s",
        (dst_org, USER, "copied"),
    ).fetchone()
    assert owns is not None
    # ...and the graph landed under its namespaced user_id.
    dst_uid = f"{USER}::space:copied"
    copied = {(r[0], r[1]) for r in _live_rows(conn, dst_org, user=dst_uid)}
    assert copied == {("f1", "space fact one"), ("f2", "space fact two")}
    edges = conn.execute(
        "SELECT src_id, dst_id, kind FROM fact_edges WHERE org_id=%s AND user_id=%s",
        (dst_org, dst_uid),
    ).fetchall()
    assert [tuple(e) for e in edges] == [("f1", "f2", "related")]
    # Source default graph untouched.
    assert len(_live_rows(conn, src_org, user=USER)) == 2


def test_copy_space_existing_target_is_409(ctx):
    client, conn, src_org, dst_org, _third = ctx
    _seed_live(conn, src_org, "f1", "source fact")
    # dst already has a space by that id.
    client_dst = client
    assert (
        client_dst.post(
            "/spaces", json={"spaceId": "copied"}, headers={"X-Praxis-Org": dst_org}
        ).status_code
        == 200
    )
    res = client.post(
        "/spaces/copy-to-org", json={"targetOrg": dst_org, "targetSpace": "copied"}
    )
    assert res.status_code == 409


def test_copy_space_invalid_slug_is_400(ctx):
    client, conn, src_org, dst_org, _third = ctx
    _seed_live(conn, src_org, "f1", "source fact")
    for bad in ["default", "co:lon", "UPPER", ""]:
        res = client.post(
            "/spaces/copy-to-org", json={"targetOrg": dst_org, "targetSpace": bad}
        )
        assert res.status_code == 400, f"{bad!r} should be rejected"


def test_copy_space_non_member_target_is_403(ctx):
    client, conn, src_org, _dst, third_org = ctx
    _seed_live(conn, src_org, "f1", "source fact")
    res = client.post(
        "/spaces/copy-to-org", json={"targetOrg": third_org, "targetSpace": "copied"}
    )
    assert res.status_code == 403


def test_copy_space_from_a_named_source_space(ctx):
    """The SOURCE can itself be a named space (selected via X-Praxis-Space)."""
    client, conn, src_org, dst_org, _third = ctx
    # Create + seed a named source space in the src org.
    assert client.post("/spaces", json={"spaceId": "work"}).status_code == 200
    src_uid = f"{USER}::space:work"
    _seed_live(conn, src_org, "s1", "fact in the work space", user=src_uid)

    res = client.post(
        "/spaces/copy-to-org",
        json={"targetOrg": dst_org, "targetSpace": "copied"},
        headers={"X-Praxis-Org": src_org, "X-Praxis-Space": "work"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["count"] == 1
    dst_uid = f"{USER}::space:copied"
    assert {r[1] for r in _live_rows(conn, dst_org, user=dst_uid)} == {
        "fact in the work space"
    }
