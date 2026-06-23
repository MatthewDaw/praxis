"""Offline tests for the candidate API server (TestClient over a temp store).

Auth is disabled via conftest (PRAXIS_AUTH_DISABLED=1) so routes flow with a dev
principal. The in-memory store has no OrgsStore, so the X-Praxis-Org header is
accepted without a membership check; we send one anyway to exercise the path.
"""

import pytest
from fastapi.testclient import TestClient

from knowledge.serve import db
from knowledge.serve.app import create_app
from knowledge.serve.store import CandidateStore, SEED_FIXTURE, contradiction_ids

ORG_HEADERS = {"X-Praxis-Org": "test-org"}


def _client(tmp_path):
    store = CandidateStore(path=tmp_path / "candidates.json", seed=SEED_FIXTURE)
    return TestClient(create_app(store), headers=ORG_HEADERS), store


def test_health_and_list(tmp_path):
    client, store = _client(tmp_path)
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["store"] == "json"
    cands = client.get("/candidates").json()
    assert isinstance(cands, list) and len(cands) > 0


def test_contradictions_endpoint_returns_pairs(tmp_path):
    client, _ = _client(tmp_path)
    pairs = client.get("/contradictions").json()
    assert len(pairs) >= 1  # the seed fixture has contradiction links
    pair = pairs[0]
    assert pair["a"]["id"] and pair["b"]["id"] and "__" in pair["id"]


def test_graph_endpoint_derives_snapshot_from_candidates(tmp_path):
    client, store = _client(tmp_path)
    graph = client.get("/graph").json()["graph"]
    assert len(graph["nodes"]) == len(store.list())
    assert graph["nodes"][0]["id"]
    assert "edges" in graph


def test_regenerate_evals_replaces_pipeline_rows_only(tmp_path):
    client, store = _client(tmp_path)
    created = client.post(
        "/candidates",
        json={
            "title": "Manual candidate",
            "content": "Keep manually-created candidates across regeneration.",
            "confidence": 0.7,
        },
    ).json()

    first = client.post("/evals/regenerate", json={"preset": "offline-fake"})
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["cases_run"] > 0
    assert first_body["insights_generated"] > 0
    assert first_body["candidates_inserted"] > 0

    after_first = store.list()
    assert any(c["id"] == created["id"] for c in after_first)
    assert sum(1 for c in after_first if c["id"].startswith("pipe_")) == first_body["candidates_inserted"]

    second = client.post("/evals/regenerate", json={"preset": "offline-fake"})
    assert second.status_code == 200
    after_second = store.list()
    assert any(c["id"] == created["id"] for c in after_second)
    assert sum(1 for c in after_second if c["id"].startswith("pipe_")) == second.json()["candidates_inserted"]

    graph = client.get("/graph").json()["graph"]
    assert len(graph["nodes"]) == len(after_second)


def test_regenerate_evals_rejects_unsupported_preset(tmp_path):
    client, _ = _client(tmp_path)
    res = client.post("/evals/regenerate", json={"preset": "expensive-live"})
    assert res.status_code == 400


def test_regenerate_evals_openrouter_is_explicitly_guarded(tmp_path):
    client, _ = _client(tmp_path)
    res = client.post("/evals/regenerate", json={"preset": "openrouter"})
    assert res.status_code == 503
    assert "PRAXIS_REGENERATE_OPENROUTER" in res.json()["detail"]


def test_promote_advances_state(tmp_path):
    client, store = _client(tmp_path)
    proposed = next(c for c in store.list() if c.get("state") == "proposed")
    res = client.post(f"/candidates/{proposed['id']}/promote", json={})
    assert res.status_code == 200
    assert res.json()["state"] == "active"  # proposed -> active (one-step funnel)


def test_reject_decays(tmp_path):
    client, store = _client(tmp_path)
    cid = store.list()[0]["id"]
    assert client.post(f"/candidates/{cid}/reject", json={"reason": "noise"}).status_code == 200
    assert store.get(cid=cid)["state"] == "decayed"


def test_create_update_delete_candidate(tmp_path):
    client, _ = _client(tmp_path)
    created = client.post(
        "/candidates",
        json={
            "title": "New lesson",
            "content": "Use typed payloads at API boundaries.",
            "confidence": 0.55,
        },
    )
    assert created.status_code == 200
    cid = created.json()["id"]
    updated = client.patch(
        f"/candidates/{cid}",
        json={"title": "New lesson (edited)"},
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "New lesson (edited)"
    deleted = client.delete(f"/candidates/{cid}")
    assert deleted.status_code == 200
    assert client.get(f"/candidates/{cid}").status_code == 404


def test_resolve_keeps_one_and_drops_link(tmp_path):
    client, store = _client(tmp_path)
    pair = client.get("/contradictions").json()[0]
    keep_id = pair["a"]["id"]
    res = client.post(f"/contradictions/{pair['id']}/resolve", json={"resolution": "keep_a", "keepId": keep_id})
    assert res.status_code == 200 and res.json()["id"] == keep_id
    # the link between the pair is gone from the kept side
    assert pair["b"]["id"] not in contradiction_ids(store.get(cid=keep_id))


def test_promote_unknown_is_404(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/candidates/nope/promote", json={}).status_code == 404


def test_metrics_endpoint_serves_fixture(tmp_path):
    client, _ = _client(tmp_path)
    metrics = client.get("/metrics").json()
    assert "correction_rate" in metrics
    assert metrics["corrections_before"] == 12


def test_me_returns_principal(tmp_path):
    client, _ = _client(tmp_path)
    me = client.get("/me").json()
    assert me["sub"] == "dev-user"
    assert me["orgs"] == []  # no OrgsStore in the in-memory path


def test_orgs_create_requires_db(tmp_path):
    client, _ = _client(tmp_path)
    res = client.post("/orgs", json={"orgId": "acme", "password": "pw"})
    assert res.status_code == 503  # orgs require a database


def test_insights_and_context_require_db(tmp_path):
    # The graph endpoints only work on the Postgres path; the in-memory store has
    # no OrgsStore, so both degrade to 503 (like the orgs routes).
    client, _ = _client(tmp_path)
    assert client.post("/insights", json={"insight": "use uv"}).status_code == 503
    assert client.get("/context", params={"query": "deps"}).status_code == 503


@pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)
def test_insight_then_context_round_trips(unique_org):
    # Real Postgres path: seed an org + membership for the dev principal, then
    # assert POST /insights lands a fact and GET /context retrieves it.
    from knowledge.serve.app import create_app as _create_app
    from knowledge.serve.orgs_store import OrgsStore

    app = _create_app()  # picks the Postgres store (DSN resolved)
    conn = db.connect()
    # Deterministic org id (from the test name) — clean any prior run so the
    # create + membership + facts start fresh and reruns stay isolated.
    conn.execute("DELETE FROM facts WHERE org_id = %s", (unique_org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (unique_org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (unique_org,))
    OrgsStore(conn).create_org(unique_org, unique_org, "pw", "dev-user")
    client = TestClient(app, headers={"X-Praxis-Org": unique_org})

    res = client.post("/insights", json={"insight": "use uv, not pip, in this repo"})
    assert res.status_code == 200
    assert res.json()["action"] in {"added", "merged", "overwrote"}

    ctx = client.get("/context", params={"query": "how do I install deps?"}).json()
    assert "uv" in ctx["context"]
