"""A project read defaults to its canonical prd-<project> snapshot, and a whole plan
re-enters in ONE bulk call.

Regression for the two-graph split: ticket STATE + completeness live on the
``prd-<project>`` snapshot (Option A), but a BARE ``incomplete_requirements(project)``
(no space/snapshot header) used to read WORKING MEMORY — a different, empty graph — so
the factory's own completeness query disagreed with the graph it wrote state to. The fix
(`_project_target`) defaults a project read to the project's plan snapshot when it exists.

Also covers the bulk-regress throughput fix: ``POST /requirements/regress`` re-enters an
arbitrary set of tickets in a single round-trip (a per-ticket record_outcome + edit loop
of ~66 calls timed out).

No OPENROUTER needed: the snapshot is seeded via a FakeEmbedder graph directly, and the
read/regress endpoints never embed.
"""

from __future__ import annotations

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"
N = 33


@pytest.fixture
def seeded(request, monkeypatch):
    """A TestClient + a prd-<project> snapshot seeded with N active requirement tickets."""
    monkeypatch.setenv("PRAXIS_AUTH_DISABLED", "1")
    from fastapi.testclient import TestClient

    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.write_step_variants import Redactor
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
            except Exception:  # noqa: BLE001 — table may not exist in a given schema
                pass

    _clean()
    OrgsStore(conn).create_org(org, org, "pw", USER)
    SpacesStore(conn).create_space(org, project, project)

    # Redact-only policy so the near-identical texts are not deduped into one fact;
    # no build_state so completeness is driven purely by recorded outcomes.
    g = PostgresVectorGraph(
        conn, org, facts_table="snapshots", space=project, snapshot=snap,
        embedder=FakeEmbedder(), recall_floor=-1.0, policy=[Redactor()],
    )
    ids = [
        g.write(f"requirement {i}: build feature {i}", state="active",
                source=snap, category="requirement", meta={"tags": ["core"]})
        for i in range(N)
    ]
    # Mark every ticket built/complete so any later incompleteness is what the test drives.
    for fid in ids:
        g.record_outcome(fid, success=True)

    client = TestClient(create_app(conn), headers={"X-Praxis-Org": org})
    yield client, org, project, snap, ids
    _clean()
    conn.close()


def _incomplete_ids(client, project):
    r = client.get("/requirements/incomplete", params={"project": project})
    assert r.status_code == 200, r.text
    return {i["id"] for i in r.json()["incomplete"]}


def test_bare_read_defaults_to_plan_snapshot(seeded):
    """A BARE incomplete_requirements(project) reads the prd-<project> snapshot (where
    state lives), not empty working memory: all-succeeded => 0 incomplete."""
    client, org, project, snap, ids = seeded
    assert _incomplete_ids(client, project) == set()  # sees the snapshot, all complete


def test_snapshot_record_outcome_reenters_via_bare_read(seeded):
    """record_outcome(False) TARGETING the snapshot (space+snapshot headers) persists and
    the ticket re-appears in a BARE incomplete read (both hit the same snapshot graph)."""
    client, org, project, snap, ids = seeded
    hdr = {"X-Praxis-Org": org, "X-Praxis-Space": project, "X-Praxis-Snapshot": snap}
    r = client.post(f"/facts/{ids[0]}/outcome", json={"success": False}, headers=hdr)
    assert r.status_code == 200, r.text
    incomplete = _incomplete_ids(client, project)
    assert ids[0] in incomplete  # regressed ticket surfaces on the bare read
    assert incomplete == {ids[0]}


def test_bulk_regress_reenters_whole_plan_in_one_call(seeded):
    """POST /requirements/regress re-enters every id in ONE call: each shows failed +
    build_state=incomplete, and a bare read then lists the whole plan as incomplete."""
    client, org, project, snap, ids = seeded
    r = client.post("/requirements/regress", json={"project": project, "ids": ids})
    assert r.status_code == 200, r.text
    assert set(r.json()["regressed"]) == set(ids)
    assert r.json()["count"] == N

    r = client.get("/requirements/incomplete", params={"project": project})
    items = {i["id"]: i for i in r.json()["incomplete"]}
    assert set(items) == set(ids)
    sample = items[ids[5]]
    assert sample["lastOutcome"] == "failed"
    assert (sample.get("meta") or {}).get("build_state") == "incomplete"
    assert sample["reason"] == "reopened"


def test_regress_requires_ids(seeded):
    client, org, project, snap, ids = seeded
    r = client.post("/requirements/regress", json={"project": project, "ids": []})
    assert r.status_code == 400
