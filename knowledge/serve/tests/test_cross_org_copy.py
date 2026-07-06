"""Integration tests for cross-org sharing: copy a snapshot or a whole space
into another org the SAME login belongs to.

Two endpoints are under test (re-keyed by the tenancy redesign to
``(space, snapshot)`` — see specs/005-praxis-tenancy-redesign/design.md §4.2/§4.4):
  * POST /snapshots/copy-to-org — copy one org-shared snapshot ``(space, snapshot)``
    into ``(targetSpace, targetSnapshot)`` in ``targetOrg`` (ids/embeddings verbatim).
  * POST /spaces/copy-to-org    — copy ALL snapshots of a source ``space`` into a
    freshly created ``targetSpace`` in ``targetOrg``.

The copy is pure SQL (no embedder/LLM), so unlike the fold-in tests these only
need a Postgres DSN — snapshot rows are seeded directly. Auth is bypassed via
conftest (PRAXIS_AUTH_DISABLED=1 -> principal sub="dev-user"); ``active_org`` still
checks membership, so the test makes dev-user a member of BOTH orgs.
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
    conn.execute("DELETE FROM snapshot_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM snapshots WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM spaces WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))


@pytest.fixture
def ctx(unique_org):
    """(client, conn, src_org, dst_org, third_org) with dev-user in the first two.

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


def _seed_snapshot(conn, org, space, snapshot, fid, text, *, scope=None, state="active"):
    conn.execute(
        "INSERT INTO snapshots (id, org_id, space, snapshot, text, scope, state) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (fid, org, space, snapshot, text, scope, state),
    )


def _seed_snapshot_edge(conn, org, space, snapshot, src, dst, *, kind="related"):
    conn.execute(
        "INSERT INTO snapshot_edges (org_id, space, snapshot, src_id, dst_id, kind) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (org, space, snapshot, src, dst, kind),
    )


def _snapshot_rows(conn, org, space, snapshot):
    return conn.execute(
        "SELECT id, text FROM snapshots WHERE org_id=%s AND space=%s AND snapshot=%s",
        (org, space, snapshot),
    ).fetchall()


# --- POST /snapshots/copy-to-org -------------------------------------------
def test_copy_snapshot_to_org_preserves_ids_and_facts(ctx):
    client, conn, src_org, dst_org, _third = ctx
    _seed_snapshot(conn, src_org, "sp", "snap", "f1", "always run the linter")
    _seed_snapshot(conn, src_org, "sp", "snap", "f2", "prefer composition")
    _seed_snapshot_edge(conn, src_org, "sp", "snap", "f1", "f2")

    res = client.post(
        "/snapshots/copy-to-org",
        json={"space": "sp", "snapshot": "snap", "targetOrg": dst_org, "targetSpace": "sp"},
    )
    assert res.status_code == 200, res.text
    assert res.json() == {
        "targetOrg": dst_org, "space": "sp", "snapshot": "snap", "count": 2,
    }

    # Copied verbatim into dst org's (sp, snap) snapshot, same ids preserved.
    copied = {(r[0], r[1]) for r in _snapshot_rows(conn, dst_org, "sp", "snap")}
    assert copied == {("f1", "always run the linter"), ("f2", "prefer composition")}
    edges = conn.execute(
        "SELECT src_id, dst_id, kind FROM snapshot_edges "
        "WHERE org_id=%s AND space=%s AND snapshot=%s",
        (dst_org, "sp", "snap"),
    ).fetchall()
    assert [tuple(e) for e in edges] == [("f1", "f2", "related")]
    # Source untouched.
    assert len(_snapshot_rows(conn, src_org, "sp", "snap")) == 2


def test_copy_snapshot_to_org_with_rename(ctx):
    client, conn, src_org, dst_org, _third = ctx
    _seed_snapshot(conn, src_org, "sp", "snap", "f1", "a fact")
    res = client.post(
        "/snapshots/copy-to-org",
        json={
            "space": "sp", "snapshot": "snap", "targetOrg": dst_org,
            "targetSpace": "sp", "targetSnapshot": "renamed",
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["snapshot"] == "renamed"
    assert len(_snapshot_rows(conn, dst_org, "sp", "renamed")) == 1
    assert len(_snapshot_rows(conn, dst_org, "sp", "snap")) == 0


def test_copy_snapshot_unknown_source_is_404(ctx):
    client, _conn, _src, dst_org, _third = ctx
    res = client.post(
        "/snapshots/copy-to-org",
        json={"space": "sp", "snapshot": "ghost", "targetOrg": dst_org, "targetSpace": "sp"},
    )
    assert res.status_code == 404


def test_copy_snapshot_existing_target_name_is_409(ctx):
    client, conn, src_org, dst_org, _third = ctx
    _seed_snapshot(conn, src_org, "sp", "snap", "f1", "source fact")
    _seed_snapshot(conn, dst_org, "sp", "snap", "x1", "already here")
    res = client.post(
        "/snapshots/copy-to-org",
        json={"space": "sp", "snapshot": "snap", "targetOrg": dst_org, "targetSpace": "sp"},
    )
    assert res.status_code == 409
    # The pre-existing target snapshot was not overwritten.
    assert {r[1] for r in _snapshot_rows(conn, dst_org, "sp", "snap")} == {"already here"}


def test_copy_snapshot_non_member_target_is_403(ctx):
    client, conn, src_org, _dst, third_org = ctx
    _seed_snapshot(conn, src_org, "sp", "snap", "f1", "source fact")
    res = client.post(
        "/snapshots/copy-to-org",
        json={"space": "sp", "snapshot": "snap", "targetOrg": third_org, "targetSpace": "sp"},
    )
    assert res.status_code == 403


def test_copy_snapshot_missing_target_org_is_400(ctx):
    client, conn, src_org, _dst, _third = ctx
    _seed_snapshot(conn, src_org, "sp", "snap", "f1", "source fact")
    res = client.post("/snapshots/copy-to-org", json={"space": "sp", "snapshot": "snap"})
    assert res.status_code == 400


# --- POST /spaces/copy-to-org ----------------------------------------------
def test_copy_space_to_org_copies_all_snapshots(ctx):
    client, conn, src_org, dst_org, _third = ctx
    # A source space must be registered (the route validates it exists) and holds
    # a snapshot with facts + an edge.
    assert client.post("/spaces", json={"spaceId": "proj"}).status_code == 200
    _seed_snapshot(conn, src_org, "proj", "snap", "f1", "space fact one")
    _seed_snapshot(conn, src_org, "proj", "snap", "f2", "space fact two")
    _seed_snapshot_edge(conn, src_org, "proj", "snap", "f1", "f2")

    res = client.post(
        "/spaces/copy-to-org",
        json={"space": "proj", "targetOrg": dst_org, "targetSpace": "copied"},
    )
    assert res.status_code == 200, res.text
    assert res.json() == {
        "targetOrg": dst_org, "space": "copied", "snapshots": 1, "count": 2,
    }

    # The space row was created in the destination org (org-shared, no owner)...
    exists = conn.execute(
        "SELECT 1 FROM spaces WHERE org_id=%s AND space_id=%s",
        (dst_org, "copied"),
    ).fetchone()
    assert exists is not None
    # ...and every snapshot's facts landed under the new space, ids preserved.
    copied = {(r[0], r[1]) for r in _snapshot_rows(conn, dst_org, "copied", "snap")}
    assert copied == {("f1", "space fact one"), ("f2", "space fact two")}
    edges = conn.execute(
        "SELECT src_id, dst_id, kind FROM snapshot_edges "
        "WHERE org_id=%s AND space=%s AND snapshot=%s",
        (dst_org, "copied", "snap"),
    ).fetchall()
    assert [tuple(e) for e in edges] == [("f1", "f2", "related")]
    # Source space untouched.
    assert len(_snapshot_rows(conn, src_org, "proj", "snap")) == 2


def test_copy_space_copies_multiple_snapshots(ctx):
    # Every snapshot in the source space is copied, not just one.
    client, conn, src_org, dst_org, _third = ctx
    assert client.post("/spaces", json={"spaceId": "proj"}).status_code == 200
    _seed_snapshot(conn, src_org, "proj", "a", "a1", "fact in snapshot a")
    _seed_snapshot(conn, src_org, "proj", "b", "b1", "fact in snapshot b")

    res = client.post(
        "/spaces/copy-to-org",
        json={"space": "proj", "targetOrg": dst_org, "targetSpace": "copied"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["snapshots"] == 2
    assert {r[1] for r in _snapshot_rows(conn, dst_org, "copied", "a")} == {"fact in snapshot a"}
    assert {r[1] for r in _snapshot_rows(conn, dst_org, "copied", "b")} == {"fact in snapshot b"}


def test_copy_space_unknown_source_is_404(ctx):
    client, _conn, _src, dst_org, _third = ctx
    res = client.post(
        "/spaces/copy-to-org",
        json={"space": "ghost", "targetOrg": dst_org, "targetSpace": "copied"},
    )
    assert res.status_code == 404


def test_copy_space_existing_target_is_409(ctx):
    client, conn, src_org, dst_org, _third = ctx
    assert client.post("/spaces", json={"spaceId": "proj"}).status_code == 200
    _seed_snapshot(conn, src_org, "proj", "snap", "f1", "source fact")
    # dst already has a space by that id.
    assert (
        client.post(
            "/spaces", json={"spaceId": "copied"}, headers={"X-Praxis-Org": dst_org}
        ).status_code
        == 200
    )
    res = client.post(
        "/spaces/copy-to-org",
        json={"space": "proj", "targetOrg": dst_org, "targetSpace": "copied"},
    )
    assert res.status_code == 409


def test_copy_space_invalid_slug_is_400(ctx):
    client, conn, src_org, dst_org, _third = ctx
    assert client.post("/spaces", json={"spaceId": "proj"}).status_code == 200
    _seed_snapshot(conn, src_org, "proj", "snap", "f1", "source fact")
    # Only '__evals__' is reserved now (the old 'default' reservation belonged to
    # the dropped working-graph-mangling model — design §4.6); ':' / uppercase /
    # empty violate the lowercase-slug rule.
    for bad in ["co:lon", "UPPER", "", "__evals__"]:
        res = client.post(
            "/spaces/copy-to-org",
            json={"space": "proj", "targetOrg": dst_org, "targetSpace": bad},
        )
        assert res.status_code == 400, f"{bad!r} should be rejected"


def test_copy_space_non_member_target_is_403(ctx):
    client, conn, src_org, _dst, third_org = ctx
    assert client.post("/spaces", json={"spaceId": "proj"}).status_code == 200
    _seed_snapshot(conn, src_org, "proj", "snap", "f1", "source fact")
    res = client.post(
        "/spaces/copy-to-org",
        json={"space": "proj", "targetOrg": third_org, "targetSpace": "copied"},
    )
    assert res.status_code == 403
