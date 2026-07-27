"""S12 bless-state guard: edit/delete against a blessed plan snapshot is refused while
no planning marker is armed, and post-bless mutations leave audit episodes naming the caller.

Tests:
  1. PATCH against a blessed plan snapshot (marker cleared) is refused
  2. DELETE against a blessed plan snapshot is refused
  3. PATCH succeeds when the planning marker is re-armed
  4. DELETE succeeds when the planning marker is re-armed
  5. Post-bless edit leaves an audit episode naming the planning_owner
  6. Post-bless delete leaves an audit episode on the planning marker
  7. Unarmed-but-never-blessed plan also refuses (no marker at all)

No OPENROUTER needed: seeding via FakeEmbedder + this test only hits the candidate
surface (PATCH/DELETE); never the semantic pipeline.
"""

from __future__ import annotations

import os
import time

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"


@pytest.fixture
def seeded(request, monkeypatch):
    """A TestClient with a prd-<project> snapshot, one requirement fact, and a planning marker."""
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
    project = "proj"
    snap = f"prd-{project}"

    db.bootstrap()
    conn = db.connect()

    def _clean():
        for t in ("fact_edges", "facts", "cached_facts", "snapshots",
                  "org_members", "orgs", "spaces"):
            try:
                conn.execute(f"DELETE FROM {t} WHERE org_id = %s", (org,))
            except Exception:
                pass

    _clean()
    OrgsStore(conn).create_org(org, org, "pw", USER)
    SpacesStore(conn).create_space(org, project, project)

    g = PostgresVectorGraph(
        conn, org, facts_table="snapshots", space=project, snapshot=snap,
        embedder=FakeEmbedder(), recall_floor=-1.0,
    )
    # Create a requirement fact in the plan snapshot
    fid = g.write("requirement: test feature X", state="active",
                  source=snap, category="requirement",
                  meta={"title": "Test Requirement", "requirement_id": "R1"})

    # Create the planning marker
    marker_id = g.ensure_planning_marker(project)

    # Arm the marker (like stamp_planning does)
    g.set_meta(marker_id, {"planning_owner": "sess-test", "planning_at": time.time()})

    app = create_app()
    client = TestClient(app, headers={"X-Praxis-Org": org})

    yield {
        "client": client,
        "org": org,
        "project": project,
        "snap": snap,
        "fact_id": fid,
        "marker_id": marker_id,
        "conn": conn,
        "graph": g,
    }

    _clean()


# --- helpers ---

def _patch(client, org, project, snap, fact_id, body):
    """PATCH a candidate in the plan snapshot."""
    h = {"X-Praxis-Org": org, "X-Praxis-Space": project, "X-Praxis-Snapshot": snap}
    return client.patch(f"/candidates/{fact_id}", json=body, headers=h)


def _delete(client, org, project, snap, fact_id):
    """DELETE a candidate from the plan snapshot."""
    h = {"X-Praxis-Org": org, "X-Praxis-Space": project, "X-Praxis-Snapshot": snap}
    return client.delete(f"/candidates/{fact_id}", headers=h)


def _bless(graph, marker_id):
    """Simulate clear_planning: NULL planning_owner/planning_at, set blessed_at."""
    existing = _marker_meta(graph, marker_id)
    existing.update({
        "planning_owner": None,
        "planning_at": None,
        "blessed_at": time.time(),
    })
    graph.set_meta(marker_id, existing)


def _rearm(graph, marker_id, owner="sess-rearm"):
    """Simulate stamp_planning: set planning_owner/planning_at (merge, preserving blessed_at)."""
    existing = _marker_meta(graph, marker_id)
    existing.update({
        "planning_owner": owner,
        "planning_at": time.time(),
    })
    graph.set_meta(marker_id, existing)


def _marker_meta(graph, marker_id):
    """Read the marker's meta."""
    marker = graph.get_fact(marker_id)
    if marker is None:
        return {}
    return dict(marker.meta or {})


# --------------------------------------------------------------------------- edit after bless

def test_edit_refused_after_bless(seeded):
    """PATCH against a blessed plan snapshot is refused."""
    _bless(seeded["graph"], seeded["marker_id"])

    resp = _patch(seeded["client"], seeded["org"], seeded["project"],
                   seeded["snap"], seeded["fact_id"],
                   {"title": "Changed Title"})

    assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"
    detail = str(resp.json().get("detail", ""))
    assert "blessed" in detail.lower() or "re-arm" in detail.lower(), (
        f"detail should mention bless/re-arm: {detail}")


def test_edit_succeeds_after_rearm(seeded):
    """PATCH succeeds after the planning marker is re-armed post-bless."""
    _bless(seeded["graph"], seeded["marker_id"])
    _rearm(seeded["graph"], seeded["marker_id"], owner="sess-rearm")

    resp = _patch(seeded["client"], seeded["org"], seeded["project"],
                   seeded["snap"], seeded["fact_id"],
                   {"title": "Changed After Rearm"})

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data.get("meta", {}).get("title") == "Changed After Rearm"


def test_edit_post_bless_leaves_audit_episode(seeded):
    """Post-bless edit records an audit episode naming the planning_owner."""
    _bless(seeded["graph"], seeded["marker_id"])
    _rearm(seeded["graph"], seeded["marker_id"], owner="sess-rearm")

    resp = _patch(seeded["client"], seeded["org"], seeded["project"],
                   seeded["snap"], seeded["fact_id"],
                   {"title": "Title With Audit"})

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    trail = data.get("meta", {}).get("auditTrail", [])
    # Should contain an "edited" entry with actor set to the planning owner
    edited_entry = [e for e in trail if e.get("action") == "edited"]
    assert edited_entry, f"expected an 'edited' audit entry in: {trail}"
    actor = edited_entry[-1].get("actor", "")
    assert actor == "sess-rearm", (
        f"post-bless edit audit actor should be the planning_owner 'sess-rearm', "
        f"got {actor!r}")


# --------------------------------------------------------------------------- delete after bless

def test_delete_refused_after_bless(seeded):
    """DELETE against a blessed plan snapshot is refused."""
    _bless(seeded["graph"], seeded["marker_id"])

    resp = _delete(seeded["client"], seeded["org"], seeded["project"],
                    seeded["snap"], seeded["fact_id"])

    assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"
    detail = str(resp.json().get("detail", ""))
    assert "blessed" in detail.lower() or "re-arm" in detail.lower(), (
        f"detail should mention bless/re-arm: {detail}")


def test_delete_succeeds_after_rearm(seeded):
    """DELETE succeeds after the planning marker is re-armed post-bless."""
    _bless(seeded["graph"], seeded["marker_id"])
    _rearm(seeded["graph"], seeded["marker_id"], owner="sess-rearm")

    resp = _delete(seeded["client"], seeded["org"], seeded["project"],
                    seeded["snap"], seeded["fact_id"])

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data.get("deleted") == seeded["fact_id"]


@pytest.mark.skipif(
    not os.getenv("OPENROUTER_API_KEY"),
    reason="the DELETE path runs through create_app()'s own graph, which uses the real "
           "embedder (the fixture's FakeEmbedder only covers direct-graph writes)",
)
def test_delete_post_bless_leaves_audit_on_marker(seeded):
    """Post-bless delete records an audit entry on the planning marker."""

    # Create a fresh fact to delete
    fid2 = seeded["graph"].write("requirement: to be deleted", state="active",
                                 source=seeded["snap"], category="requirement",
                                 meta={"title": "Delete Me", "requirement_id": "R2"})

    _bless(seeded["graph"], seeded["marker_id"])
    _rearm(seeded["graph"], seeded["marker_id"], owner="sess-rearm-del")

    resp = _delete(seeded["client"], seeded["org"], seeded["project"],
                    seeded["snap"], fid2)

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"

    # Verify audit on marker
    mm = _marker_meta(seeded["graph"], seeded["marker_id"])
    trail = mm.get("auditTrail", [])
    delete_entries = [e for e in trail if "delete" in str(e.get("action", ""))]
    assert delete_entries, (
        f"expected a post-bless-delete audit entry on marker, got trail: {trail}")
    actor = delete_entries[-1].get("actor", "")
    assert actor == "sess-rearm-del", (
        f"post-bless delete audit actor should be planning_owner, got {actor!r}")


# --------------------------------------------------------------------------- no marker at all

def test_edit_refused_when_no_marker(seeded):
    """PATCH is refused when no planning marker exists for the project (never planned)."""
    # Delete the marker
    seeded["graph"].delete_fact(seeded["marker_id"])

    resp = _patch(seeded["client"], seeded["org"], seeded["project"],
                   seeded["snap"], seeded["fact_id"],
                   {"title": "No Marker"})

    assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"
    detail = str(resp.json().get("detail", ""))
    assert "marker" in detail.lower() or "re-arm" in detail.lower(), (
        f"detail should mention missing marker: {detail}")


# --------------------------------------------------------------------------- active without bless

def test_edit_allowed_while_active(seeded):
    """PATCH succeeds while the planning marker is armed (never blessed)."""
    # Marker is already armed from fixture setup
    resp = _patch(seeded["client"], seeded["org"], seeded["project"],
                   seeded["snap"], seeded["fact_id"],
                   {"title": "Active Edit"})

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data.get("meta", {}).get("title") == "Active Edit"


# --------------------------------------------------------------------------- stale marker

def test_edit_refused_when_marker_stale(seeded):
    """PATCH is refused when the planning marker is stale (TTL expired)."""
    # Age the marker past TTL
    stale_time = time.time() - 3700  # 3600s TTL + 100s buffer
    seeded["graph"].set_meta(seeded["marker_id"], {
        "planning_owner": "sess-stale",
        "planning_at": stale_time,
    })

    resp = _patch(seeded["client"], seeded["org"], seeded["project"],
                   seeded["snap"], seeded["fact_id"],
                   {"title": "Stale Marker"})

    assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text}"
    detail = str(resp.json().get("detail", ""))
    assert "stale" in detail.lower() or "re-arm" in detail.lower(), (
        f"detail should mention stale marker: {detail}")


# --------------------------------------------------------------------------- non-prd snapshot (no guard)

def test_edit_allowed_on_non_prd_snapshot(seeded):
    """PATCH on a non-prd-* snapshot is NOT guarded — only plan snapshots are."""
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder

    # Create a building-validation snapshot (not prd-*)
    other_snap = "building-validation"
    g2 = PostgresVectorGraph(
        seeded["conn"], seeded["org"],
        facts_table="snapshots", space=seeded["project"], snapshot=other_snap,
        embedder=FakeEmbedder(), recall_floor=-1.0,
    )
    fid_check = g2.write("check: validate something", state="active",
                         source=other_snap, category="check", scope="validation",
                         meta={"title": "Test Check"})

    # This should succeed even without a planning marker — only prd-* is guarded
    h = {"X-Praxis-Org": seeded["org"],
         "X-Praxis-Space": seeded["project"],
         "X-Praxis-Snapshot": other_snap}
    resp = seeded["client"].patch(f"/candidates/{fid_check}",
                                  json={"title": "Check Update"}, headers=h)
    assert resp.status_code == 200, (
        f"edit on non-prd snapshot should not be guarded, "
        f"got {resp.status_code}: {resp.text}")


def test_planning_marker_itself_is_exempt_from_the_bless_guard():
    """The re-arm path must not be blocked by the guard it re-arms.

    REGRESSION: `_check_bless_guard` applied to every fact in a `prd-*` snapshot, including
    the planning marker. `stamp_planning` re-arms a blessed plan by PATCHing that marker, so
    a blessed plan could never be re-armed: the refusal said "re-arm the planning marker
    (stamp_planning)" and stamp_planning was refused by the same check. A blessed plan was
    permanently immutable, with no documented escape.
    """
    from knowledge.serve.facts_candidates import FactsCandidates

    class _G:
        def find_planning_marker(self, project):
            return None  # blessed / unarmed: any guarded fact must raise here

    store = object.__new__(FactsCandidates)
    store._facts_table = "snapshots"
    store._snapshot = "prd-proj"
    store._space = "proj"
    store.graph = _G()

    # The marker itself is exempt -> allowed even with no armed marker.
    assert store._check_bless_guard("prd-proj::planning", "edit") == (False, "")

    # Ordinary plan facts are still guarded.
    with pytest.raises(ValueError, match="planning marker"):
        store._check_bless_guard("some-ordinary-fact-id", "edit")
