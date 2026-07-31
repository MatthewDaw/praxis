"""``POST /planning-marker`` arms and disarms the bless-state guard over HTTP.

The guard that protects a blessed ``prd-<project>`` plan refuses an edit with "re-arm the
planning marker (stamp_planning) to mutate this snapshot" — but arming used to live ONLY in
``agent_factory.hooks._ticket_state.stamp_planning``, a Python helper reachable from the hooks
and from nothing else. An agent working through the HTTP/MCP surface was handed an instruction
it had no way to follow, and reasonably concluded that snapshot facts were simply not editable.

These tests pin the full round trip the guard's own error message now promises:
arm -> edit succeeds -> re-bless -> edit refused again.
"""

from __future__ import annotations

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
    """A blessed plan snapshot: one requirement fact and a marker with no planning owner."""
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
        for t in ("fact_edges", "facts", "snapshots", "org_members", "orgs", "spaces"):
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
    fid = g.write("requirement: test feature X", state="active", source=snap,
                  category="requirement",
                  meta={"title": "Test Requirement", "requirement_id": "R1"})
    marker_id = g.ensure_planning_marker(project)
    # BLESSED: owner cleared, blessed_at recorded. This is the resting state of a finished
    # plan — and the state an operator naturally describes as "the marker was cleared", which
    # is exactly the phrasing that gets misread as "unprotected".
    g.set_meta(marker_id, {"planning_owner": None, "planning_at": None,
                           "blessed_at": time.time()})

    app = create_app()
    headers = {"X-Praxis-Org": org, "X-Praxis-Space": project, "X-Praxis-Snapshot": snap}
    yield {
        "client": TestClient(app, headers={"X-Praxis-Org": org}),
        "headers": headers, "project": project, "fact_id": fid,
        "marker_id": marker_id, "graph": g,
    }
    _clean()


def _marker_meta(graph, marker_id):
    return dict(getattr(graph.get_fact(marker_id), "meta", None) or {})


def test_arm_then_edit_then_rebless(seeded):
    """The round trip the guard's error message promises, end to end."""
    c, h, fid = seeded["client"], seeded["headers"], seeded["fact_id"]

    # Blessed: refused, and the detail names the remedy rather than being a bare 400.
    refused = c.patch(f"/candidates/{fid}", json={"content": "edited"}, headers=h)
    assert refused.status_code == 400
    assert "planning marker" in refused.json()["detail"]

    armed = c.post("/planning-marker",
                   json={"project": seeded["project"], "owner": "sess-repair"}, headers=h)
    assert armed.status_code == 200 and armed.json()["armed"] is True

    ok = c.patch(f"/candidates/{fid}", json={"content": "edited"}, headers=h)
    assert ok.status_code == 200 and ok.json()["content"] == "edited"

    reblessed = c.post("/planning-marker",
                       json={"project": seeded["project"], "clear": True}, headers=h)
    assert reblessed.status_code == 200 and reblessed.json()["armed"] is False

    # Re-protected: the plan must not be left open after a repair.
    again = c.patch(f"/candidates/{fid}", json={"content": "second edit"}, headers=h)
    assert again.status_code == 400
    # ...and the refused edit changed nothing.
    assert c.get(f"/candidates/{fid}", headers=h).json()["content"] == "edited"


def test_arming_sets_owner_and_reblessing_records_blessed_at(seeded):
    """Arming/disarming write the exact fields the guard reads — not merely 'some' meta."""
    c, h, g, mid = seeded["client"], seeded["headers"], seeded["graph"], seeded["marker_id"]

    c.post("/planning-marker", json={"project": seeded["project"], "owner": "sess-x"}, headers=h)
    meta = _marker_meta(g, mid)
    assert meta["planning_owner"] == "sess-x"
    assert isinstance(meta["planning_at"], (int, float))

    c.post("/planning-marker", json={"project": seeded["project"], "clear": True}, headers=h)
    meta = _marker_meta(g, mid)
    assert meta["planning_owner"] is None and meta["planning_at"] is None
    assert isinstance(meta["blessed_at"], (int, float))


def test_meta_only_edit_works_once_armed(seeded):
    """A meta-only amendment (depends_on/tags/scope) has no identity-keyed upsert to fall back
    on, so the marker is the ONLY route to it — the case that makes this endpoint load-bearing
    rather than a convenience."""
    c, h, fid = seeded["client"], seeded["headers"], seeded["fact_id"]
    c.post("/planning-marker", json={"project": seeded["project"], "owner": "s"}, headers=h)

    before = c.get(f"/candidates/{fid}", headers=h).json()["content"]
    res = c.patch(f"/candidates/{fid}", json={"meta": {"depends_on": ["R0"]}}, headers=h)
    assert res.status_code == 200
    assert res.json()["meta"]["depends_on"] == ["R0"]
    # The deliberate wording of the statement must survive a meta-only edit untouched.
    assert res.json()["content"] == before


def test_ensure_without_owner_or_clear_changes_no_state(seeded):
    """The bootstrap call stays a pure find-or-create: it must not silently unlock a blessed
    plan just because someone ensured the marker exists."""
    c, h, g, mid = seeded["client"], seeded["headers"], seeded["graph"], seeded["marker_id"]
    before = _marker_meta(g, mid)

    res = c.post("/planning-marker", json={"project": seeded["project"]}, headers=h)
    assert res.status_code == 200 and res.json()["armed"] is None
    assert _marker_meta(g, mid) == before

    assert c.patch(f"/candidates/{seeded['fact_id']}",
                   json={"content": "nope"}, headers=h).status_code == 400


def test_owner_and_clear_together_is_refused(seeded):
    """Arming and disarming in one call has no coherent meaning; guessing an order would make
    the plan's protection state depend on an implementation detail."""
    res = seeded["client"].post(
        "/planning-marker",
        json={"project": seeded["project"], "owner": "s", "clear": True},
        headers=seeded["headers"],
    )
    assert res.status_code == 400
    assert "not both" in res.json()["detail"]
