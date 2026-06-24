"""Integration tests for the skill-sharing routes (browse + fold-in).

Like test_server.py, the server writes through the facade's REAL embedder and a
real ConflictJudge (no fakes injected by create_app), so these tests need both a
Postgres DSN and an OPENROUTER_API_KEY — fold-in embeds and judges for real.

Auth is bypassed via conftest (PRAXIS_AUTH_DISABLED=1 -> principal sub="dev-user").
The caller is always "dev-user"; a second member "userB" is added to the same org
and seeded with facts directly so dev-user can browse and fold them in.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (  # noqa: E402
    PostgresVectorGraph,
    default_write_policy,
)
from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None or not os.getenv("OPENROUTER_API_KEY"),
    reason=(
        "needs a Postgres DSN (PRAXIS_DB_URL / AWS secret) AND OPENROUTER_API_KEY — "
        "fold-in embeds candidates and judges conflicts via the real embedder/LLM"
    ),
)

USER = "dev-user"
USER_B = "userB"


def _wipe(conn, org):
    conn.execute("DELETE FROM fact_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM cached_fact_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM cached_facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))


@pytest.fixture
def ctx(unique_org):
    """(client, conn, org) over a fresh org with dev-user (owner) + userB members."""
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


def _seed_b_fact(conn, org, fid, text, *, scope=None, state="active"):
    conn.execute(
        """
        INSERT INTO facts (id, org_id, user_id, text, scope, state)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (fid, org, USER_B, text, scope, state),
    )


def _seed_b_edge(conn, org, src, dst, kind="contradiction"):
    conn.execute(
        "INSERT INTO fact_edges (org_id, user_id, src_id, dst_id, kind) VALUES (%s,%s,%s,%s,%s)",
        (org, USER_B, src, dst, kind),
    )


def _seed_b_cached(conn, org, key, fid, text, *, scope=None, state="active"):
    conn.execute(
        """
        INSERT INTO cached_facts (id, org_id, user_id, cache_key, text, scope, state)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (fid, org, USER_B, key, text, scope, state),
    )


def _seed_b_cached_edge(conn, org, key, src, dst, kind="contradiction"):
    conn.execute(
        """
        INSERT INTO cached_fact_edges (org_id, user_id, cache_key, src_id, dst_id, kind)
        VALUES (%s,%s,%s,%s,%s,%s)
        """,
        (org, USER_B, key, src, dst, kind),
    )


# --- GET /org/sources ------------------------------------------------------
def test_org_sources_lists_members_and_snapshots(ctx):
    client, conn, org = ctx
    _seed_b_cached(conn, org, "snapshot:snapB", "b1", "snapshot fact")
    body = client.get("/org/sources").json()
    by_user = {s["userId"]: s for s in body["sources"]}
    assert set(by_user) == {USER, USER_B}
    assert by_user[USER]["isSelf"] is True
    assert by_user[USER_B]["isSelf"] is False
    assert by_user[USER]["role"] == "owner"
    assert "snapB" in by_user[USER_B]["snapshots"]


# --- GET /org/sources/{user_id}/snapshots/{name}/facts ---------------------
def test_browse_member_snapshot_facts_grouped(ctx):
    client, conn, org = ctx
    _seed_b_cached(conn, org, "snapshot:snapB", "b1", "skill one in alpha", scope="alpha")
    _seed_b_cached(conn, org, "snapshot:snapB", "b2", "skill two in alpha", scope="alpha")
    _seed_b_cached(conn, org, "snapshot:snapB", "b3", "lonely skill", scope="beta")
    body = client.get(f"/org/sources/{USER_B}/snapshots/snapB/facts").json()
    assert body["userId"] == USER_B
    assert body["snapshot"] == "snapB"
    groups = {g["key"]: g for g in body["groups"]}
    assert set(groups) == {"alpha", "beta"}
    assert {f["text"] for f in groups["alpha"]["facts"]} == {
        "skill one in alpha",
        "skill two in alpha",
    }


def test_browse_non_member_target_is_404(ctx):
    client, _conn, _org = ctx
    assert client.get("/org/sources/stranger/snapshots/x/facts").status_code == 404


def test_browse_unknown_snapshot_is_404(ctx):
    client, _conn, _org = ctx
    assert client.get(f"/org/sources/{USER_B}/snapshots/nope/facts").status_code == 404


def test_browse_non_member_org_header_is_403(ctx):
    client, _conn, _org = ctx
    res = client.get(
        f"/org/sources/{USER_B}/snapshots/x/facts", headers={"X-Praxis-Org": "not-my-org"}
    )
    assert res.status_code == 403


def test_browse_snapshot_reads_cached_not_live(ctx):
    client, conn, org = ctx
    _seed_b_fact(conn, org, "b1", "live only fact")
    _seed_b_cached(conn, org, "snapshot:snapB", "c1", "snapshot only fact")
    body = client.get(f"/org/sources/{USER_B}/snapshots/snapB/facts").json()
    texts = {f["text"] for g in body["groups"] for f in g["facts"]}
    assert texts == {"snapshot only fact"}


# --- POST /fold-in ---------------------------------------------------------
def _caller_facts(conn, org):
    rows = conn.execute(
        "SELECT id, text, state, meta FROM facts WHERE org_id=%s AND user_id=%s",
        (org, USER),
    ).fetchall()
    return rows


SNAP = "snapB"
SNAP_KEY = "snapshot:snapB"


def test_fold_in_copies_facts_with_provenance_active(ctx):
    client, conn, org = ctx
    _seed_b_cached(conn, org, SNAP_KEY, "b1", "always run the linter before committing")
    res = client.post(
        "/fold-in", json={"sourceUser": USER_B, "snapshot": SNAP, "factIds": ["b1"]}
    )
    assert res.status_code == 200, res.text
    assert res.json()["folded"] == 1
    assert res.json()["mode"] == "add"
    rows = _caller_facts(conn, org)
    assert len(rows) == 1
    _id, text, state, meta = rows[0]
    assert text == "always run the linter before committing"
    assert state == "active"  # explicit user action lands active
    assert meta["foldedFrom"] == {"userId": USER_B, "source": SNAP_KEY}
    assert meta["foldedFromFactId"] == "b1"


def test_fold_in_dedups_identical_fact_caller_already_holds(ctx):
    client, conn, org = ctx
    text = "use uv run pytest to run the test suite"
    # Caller already holds the fact.
    client.post("/insights", json={"insight": text})
    before = len(_caller_facts(conn, org))
    assert before == 1
    _seed_b_cached(conn, org, SNAP_KEY, "b1", text)
    res = client.post(
        "/fold-in", json={"sourceUser": USER_B, "snapshot": SNAP, "factIds": ["b1"]}
    ).json()
    assert res["deduped"] >= 1
    assert res["folded"] == 0
    # No duplicate row created.
    assert len(_caller_facts(conn, org)) == before


def test_fold_in_contradiction_reports_conflict_without_overwrite(ctx):
    client, conn, org = ctx
    held = "the deploy day for this repo is friday"
    rival = "the deploy day for this repo is monday"
    # Seed the caller's held fact through the claim-extracting pipeline so it has
    # rows in the `claims` table — the structural ClaimConflictDetector only flags
    # a clash against a fact that has extracted claims. (/insights uses the
    # ConflictOverwriter path and does not extract claims.)
    caller_graph = PostgresVectorGraph(conn, org, USER, policy=default_write_policy())
    caller_graph.write(held, state="active")
    _seed_b_cached(conn, org, SNAP_KEY, "b1", rival)
    res = client.post(
        "/fold-in", json={"sourceUser": USER_B, "snapshot": SNAP, "factIds": ["b1"]}
    ).json()
    # The conflicting fact is added (flagged), not silently merged/overwritten.
    assert res["conflicts"], res
    texts = {r[1] for r in _caller_facts(conn, org)}
    # Both the original and the folded contradicting fact survive (no overwrite).
    assert held in texts
    assert rival in texts


def test_fold_in_carries_edge_between_two_selected_facts(ctx):
    client, conn, org = ctx
    _seed_b_cached(conn, org, SNAP_KEY, "b1", "prefer composition over inheritance for reuse")
    _seed_b_cached(conn, org, SNAP_KEY, "b2", "keep modules small and focused on one job")
    _seed_b_cached(conn, org, SNAP_KEY, "b3", "an unrelated outside fact not selected")
    _seed_b_cached_edge(conn, org, SNAP_KEY, "b1", "b2", kind="related")  # both -> carried
    _seed_b_cached_edge(conn, org, SNAP_KEY, "b2", "b3", kind="related")  # b3 out -> skipped
    res = client.post(
        "/fold-in", json={"sourceUser": USER_B, "snapshot": SNAP, "factIds": ["b1", "b2"]}
    )
    assert res.status_code == 200, res.text
    # New caller fact ids, then check the 'related' edge was remapped onto them.
    id_by_text = {r[1]: r[0] for r in _caller_facts(conn, org)}
    new_b1 = id_by_text["prefer composition over inheritance for reuse"]
    new_b2 = id_by_text["keep modules small and focused on one job"]
    edges = conn.execute(
        "SELECT src_id, dst_id, kind FROM fact_edges WHERE org_id=%s AND user_id=%s AND kind='related'",
        (org, USER),
    ).fetchall()
    assert (new_b1, new_b2, "related") in [tuple(e) for e in edges]
    # The edge to the unselected b3 was not carried.
    assert all("related" == e[2] and {e[0], e[1]} == {new_b1, new_b2} for e in edges)


def test_fold_in_from_snapshot_reads_cached_not_live(ctx):
    client, conn, org = ctx
    _seed_b_fact(conn, org, "shared1", "this is the LIVE version of the fact")
    _seed_b_cached(conn, org, SNAP_KEY, "shared1", "this is the SNAPSHOT version")
    res = client.post(
        "/fold-in",
        json={"sourceUser": USER_B, "snapshot": SNAP, "factIds": ["shared1"]},
    )
    assert res.status_code == 200, res.text
    texts = {r[1] for r in _caller_facts(conn, org)}
    assert "this is the SNAPSHOT version" in texts
    assert "this is the LIVE version of the fact" not in texts


def test_fold_in_replace_mode_truncates_caller_graph_first(ctx):
    client, conn, org = ctx
    # Caller already holds two unrelated facts.
    caller_graph = PostgresVectorGraph(conn, org, USER, policy=default_write_policy())
    caller_graph.write("an old fact the caller already had", state="active")
    caller_graph.write("a second old caller fact", state="active")
    assert len(_caller_facts(conn, org)) == 2
    _seed_b_cached(conn, org, SNAP_KEY, "b1", "the only fact after a replace fold-in")
    res = client.post(
        "/fold-in",
        json={"sourceUser": USER_B, "snapshot": SNAP, "factIds": ["b1"], "mode": "replace"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["mode"] == "replace"
    texts = {r[1] for r in _caller_facts(conn, org)}
    assert texts == {"the only fact after a replace fold-in"}


def test_fold_in_unknown_source_user_is_404(ctx):
    client, _conn, _org = ctx
    res = client.post(
        "/fold-in", json={"sourceUser": "stranger", "snapshot": SNAP, "factIds": ["x"]}
    )
    assert res.status_code == 404


def test_fold_in_empty_fact_ids_is_400(ctx):
    client, _conn, _org = ctx
    res = client.post(
        "/fold-in", json={"sourceUser": USER_B, "snapshot": SNAP, "factIds": []}
    )
    assert res.status_code == 400


def test_fold_in_missing_snapshot_is_400(ctx):
    client, _conn, _org = ctx
    res = client.post("/fold-in", json={"sourceUser": USER_B, "factIds": ["b1"]})
    assert res.status_code == 400
