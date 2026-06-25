"""Integration tests for the candidate API server over the facts spine.

The server is Postgres-only and writes through the facade's REAL embedder
(``create_app`` injects no fake), so these tests require both a Postgres DSN and
an OPENROUTER_API_KEY — POST /insights and POST /candidates embed for real.

Auth is bypassed via conftest (PRAXIS_AUTH_DISABLED=1 -> principal sub="dev-user").
``active_org`` always checks org membership, so each test gets a unique throwaway
org with "dev-user" added as a member and sends it via the X-Praxis-Org header.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

# Load the repo-root .env so PRAXIS_DB_URL / OPENROUTER_API_KEY resolve at
# import time (the module-level skipif below reads them).
load_dotenv()

from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None or not os.getenv("OPENROUTER_API_KEY"),
    reason=(
        "needs a Postgres DSN (PRAXIS_DB_URL / AWS secret) AND OPENROUTER_API_KEY — "
        "the HTTP write path embeds candidates/insights via the real embedder"
    ),
)

USER = "dev-user"


@pytest.fixture
def client(unique_org):
    """A TestClient over a fresh throwaway tenant (org + dev-user membership)."""
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    # Start clean so create + membership + facts begin fresh and reruns isolate.
    conn.execute("DELETE FROM fact_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM cached_facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", USER)
    app = create_app(conn)
    yield TestClient(app, headers={"X-Praxis-Org": org})
    conn.execute("DELETE FROM fact_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM cached_facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    conn.close()


def _create(client, title="A lesson", content="Use typed payloads at API boundaries."):
    res = client.post("/candidates", json={"title": title, "content": content})
    assert res.status_code == 200, res.text
    return res.json()


def test_health_reports_postgres(client):
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["store"] == "postgres"


def test_create_list_get_candidate(client):
    created = _create(client)
    cid = created["id"]
    assert created["state"] == "proposed"

    listed = client.get("/candidates").json()
    assert any(c["id"] == cid for c in listed)

    got = client.get(f"/candidates/{cid}")
    assert got.status_code == 200
    assert got.json()["title"] == "A lesson"


def test_insight_persists_writer_metadata(client):
    """H12: writer-supplied source/scope/category/meta round-trip unchanged.

    A value written via POST /insights must come back on /context hits
    (source/scope/category) and on /candidates (meta) — today they are dropped.
    """
    r = client.post("/insights", json={
        "insight": "The team day resets at 03:00 local time.",
        "source": "prd-team-app", "scope": "prd-team-app",
        "category": "requirement", "meta": {"requirement_id": "R4"}})
    assert r.status_code == 200, r.text
    hit = next(h for h in client.get("/context", params={"query": "team day reset"}).json()["hits"]
               if "03:00" in h["text"])
    assert hit["source"] == "prd-team-app"          # RED today: null
    assert hit["scope"] == "prd-team-app"
    assert hit["category"] == "requirement"
    cand = next(c for c in client.get("/candidates").json() if "03:00" in c["content"])
    assert cand.get("meta", {}).get("requirement_id") == "R4"


def test_insight_derived_fills_unset_metadata(client):
    """H12 precedence: writer value wins; a field left unset is not clobbered.

    Omitting category lets ingestion-derived values fill it (or stay null) — the
    point is the explicit fields still round-trip and the omitted one never
    overwrites a writer-set sibling.
    """
    r = client.post("/insights", json={
        "insight": "Backups run nightly at 02:00 UTC.",
        "scope": "ops"})
    assert r.status_code == 200, r.text
    hit = next(h for h in client.get("/context", params={"query": "backups nightly"}).json()["hits"]
               if "02:00" in h["text"])
    assert hit["scope"] == "ops"


def test_insights_batch_writes_all_and_confirms_retrievable(client):
    """H8: one round-trip writes many shaped facts, each confirmed read-back-able.

    The batch endpoint returns one result per item (in order), each persisting its
    H12 metadata and carrying a ``retrievable`` flag that proves read-your-writes —
    the just-written fact is immediately found by /context.
    """
    r = client.post("/insights/batch", json={
        "insights": [
            {"insight": "The deploy pipeline runs on push to main.",
             "source": "ops-runbook", "category": "pattern"},
            {"insight": "Staging mirrors prod but uses a seeded database.",
             "scope": "staging", "meta": {"doc": "D2"}},
            {"insight": "Secrets are sourced from AWS Secrets Manager at boot."},
        ]})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["count"] == 3
    assert all(res["ok"] for res in payload["results"])
    assert all(res["retrievable"] for res in payload["results"])
    assert all(res["id"] for res in payload["results"])

    # Read-your-writes: every batched fact is immediately retrievable + metadata stuck.
    hit = next(h for h in client.get("/context", params={"query": "deploy pipeline push"}).json()["hits"]
               if "push to main" in h["text"])
    assert hit["source"] == "ops-runbook"
    assert hit["category"] == "pattern"
    cand = next(c for c in client.get("/candidates").json() if "seeded database" in c["content"])
    assert cand.get("meta", {}).get("doc") == "D2"


def test_insights_batch_bad_item_does_not_abort_batch(client):
    """H8: a single malformed item fails cleanly; the good items still land."""
    r = client.post("/insights/batch", json={
        "insights": [
            {"insight": "Rate limits reset at the top of each minute."},
            {"insight": "   "},  # empty -> per-item error, not a 500
            {"insight": "The cache TTL is five minutes."},
        ]})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results[0]["ok"] is True and results[0]["retrievable"]
    assert results[1]["ok"] is False and "insight required" in results[1]["error"]
    assert results[2]["ok"] is True and results[2]["retrievable"]


def test_insights_batch_requires_nonempty_list(client):
    assert client.post("/insights/batch", json={"insights": []}).status_code == 400
    assert client.post("/insights/batch", json={}).status_code == 400


def test_promote_advances_proposed_to_active(client):
    cid = _create(client)["id"]
    res = client.post(f"/candidates/{cid}/promote", json={})
    assert res.status_code == 200
    assert res.json()["state"] == "active"


def test_reject_rejects(client):
    cid = _create(client)["id"]
    res = client.post(f"/candidates/{cid}/reject", json={"reason": "noise"})
    assert res.status_code == 200
    assert res.json()["state"] == "rejected"


def test_record_outcome_accepts_boolean(client):
    fid = _create(client)["id"]  # candidate id IS the fact id
    res = client.post(f"/facts/{fid}/outcome", json={"success": False})
    assert res.status_code == 200
    assert res.json() == {"id": fid, "success": False}


def test_record_outcome_requires_boolean_success(client):
    fid = _create(client)["id"]
    res = client.post(f"/facts/{fid}/outcome", json={})
    assert res.status_code == 400


def test_patch_updates_candidate(client):
    cid = _create(client, title="New lesson")["id"]
    res = client.patch(f"/candidates/{cid}", json={"title": "New lesson (edited)"})
    assert res.status_code == 200
    assert res.json()["title"] == "New lesson (edited)"


def test_delete_active_is_refused_with_409(client):
    cid = _create(client)["id"]
    client.post(f"/candidates/{cid}/promote")  # -> active
    res = client.delete(f"/candidates/{cid}")
    assert res.status_code == 409
    assert "reject" in res.json()["detail"].lower()
    assert client.get(f"/candidates/{cid}").status_code == 200  # still there


def test_delete_proposed_and_rejected_succeed(client):
    proposed = _create(client, title="P", content="A proposed note to remove.")["id"]
    assert client.delete(f"/candidates/{proposed}").status_code == 200
    rejected = _create(client, title="R", content="A note to reject then remove.")["id"]
    client.post(f"/candidates/{rejected}/reject")
    assert client.delete(f"/candidates/{rejected}").status_code == 200


def test_delete_then_get_is_404(client):
    cid = _create(client)["id"]
    assert client.delete(f"/candidates/{cid}").status_code == 200
    assert client.get(f"/candidates/{cid}").status_code == 404


def test_promote_unknown_is_404(client):
    assert client.post("/candidates/nope/promote", json={}).status_code == 404


def test_graph_reflects_active_facts(client):
    cid = _create(client, content="Prefer dependency injection at boundaries.")["id"]
    # Proposed facts are not in the active graph.
    before = client.get("/graph").json()["graph"]
    assert not any(n["id"] == cid for n in before["nodes"])
    # Promote -> it becomes an active node.
    client.post(f"/candidates/{cid}/promote", json={})
    after = client.get("/graph").json()["graph"]
    assert any(n["id"] == cid for n in after["nodes"])
    assert "edges" in after


def test_snapshots_save_list_load_delete_round_trip(client):
    cid = _create(client, content="A fact worth snapshotting in the graph.")["id"]
    client.post(f"/candidates/{cid}/promote", json={})

    saved = client.post("/snapshots", json={"name": "snap1"})
    assert saved.status_code == 200
    assert saved.json()["count"] >= 1

    listed = client.get("/snapshots").json()["snapshots"]
    assert any(s["name"] == "snap1" for s in listed)

    loaded = client.post("/snapshots/snap1/load")
    assert loaded.status_code == 200
    assert loaded.json()["loaded"] >= 1

    deleted = client.delete("/snapshots/snap1")
    assert deleted.status_code == 200
    assert not any(s["name"] == "snap1" for s in client.get("/snapshots").json()["snapshots"])


def test_load_unknown_snapshot_is_404(client):
    assert client.post("/snapshots/nope/load").status_code == 404


def test_evals_cached_shape(client):
    body = client.get("/evals/cached").json()
    assert "cached" in body
    assert isinstance(body["cached"], list)
    assert isinstance(body.get("counts", {}), dict)


def test_graph_state_all_includes_proposed(client):
    # A proposed candidate is staged out of the active graph...
    client.post("/candidates", json={"title": "t", "content": "staged proposed fact here"})
    assert client.get("/graph").json()["graph"]["nodes"] == []  # active-only default
    # ...but state=all surfaces it.
    nodes = client.get("/graph", params={"state": "all"}).json()["graph"]["nodes"]
    assert any(n["state"] == "proposed" for n in nodes)


def test_clear_graph_empties_the_users_graph(client):
    client.post("/insights", json={"insight": "deploy on fridays is fine here"})
    assert client.get("/graph").json()["graph"]["nodes"]  # non-empty before

    res = client.post("/graph/clear")
    assert res.status_code == 200
    assert res.json()["cleared"] >= 1

    assert client.get("/graph").json()["graph"]["nodes"] == []


def test_insight_then_context_round_trips(client):
    res = client.post("/insights", json={"insight": "use uv, not pip, in this repo"})
    assert res.status_code == 200
    assert res.json()["action"] in {"added", "merged"}

    ctx = client.get("/context", params={"query": "how do I install deps?"}).json()
    assert "uv" in ctx["context"]


def test_insights_rejects_unknown_on_conflict(client):
    res = client.post(
        "/insights", json={"insight": "anything", "onConflict": "bogus"}
    )
    assert res.status_code == 400
    assert "onConflict" in res.json()["detail"]


def test_insights_surface_mode_keeps_both_and_surfaces_contradiction(client):
    # auto_resolve (default) silently overwrites; surface keeps both facts and raises
    # a PENDING contradiction for human adjudication (the factory plan-hardening loop).
    first = client.post(
        "/insights",
        json={"insight": "The factory's default rate limit is 100 requests per second."},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/insights",
        json={
            "insight": "The factory's default rate limit is 500 requests per second.",
            "onConflict": "surface",
        },
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["onConflict"] == "surface"
    assert body["contradictionsSurfaced"] >= 1
    assert body["action"] == "surfaced"

    # The clash shows up for review (vs auto_resolve, where it would be empty)...
    clusters = client.get("/contradictions").json()
    assert clusters, "surface mode should leave a pending contradiction to adjudicate"

    # ...and neither side was rejected (both kept; FR-005 only demotes to proposed).
    states = {
        c["content"]: c["state"]
        for c in client.get("/candidates").json()
        if "rate limit" in c["content"]
    }
    assert states, "both rate-limit facts should still exist"
    assert "rejected" not in states.values(), f"surface must not reject a side: {states}"

    # resolve then settles it: keep the 500 rps side, retire 100 rps.
    pair = clusters[0]["pairs"][0]
    keep = next(s["id"] for s in (pair["a"], pair["b"]) if "500" in s["content"])
    res = client.post(f"/contradictions/{pair['id']}/resolve", json={"keep": [keep]})
    assert res.status_code == 200, res.text
    ctx = client.get("/context", params={"query": "rate limit"}).json()
    assert "500" in ctx["context"] and "100 requests per second" not in ctx["context"]


def _surface_rate_limit_pair(client):
    """Seed two conflicting rate-limit facts in surface mode; return the pending pair."""
    first = client.post(
        "/insights",
        json={"insight": "The factory's default rate limit is 100 requests per second."},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/insights",
        json={
            "insight": "The factory's default rate limit is 500 requests per second.",
            "onConflict": "surface",
        },
    )
    assert second.status_code == 200, second.text
    clusters = client.get("/contradictions").json()
    assert clusters, "surface mode should leave a pending contradiction"
    return clusters[0]["pairs"][0]


def test_resolve_keep_all_keeps_both_active_and_clears_pending(client):
    # H11: keep="all" — a FALSE POSITIVE (two facts that both actually hold).
    # Non-lossy: neither side is rejected or merged; both stay active and the
    # pending pair drops out of /contradictions.
    pair = _surface_rate_limit_pair(client)

    res = client.post(f"/contradictions/{pair['id']}/resolve", json={"keep": "all"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert {f["state"] for f in body["kept"]} == {"active"}
    assert body["rejected"] == []

    assert client.get("/contradictions").json() == []
    states = {
        c["content"]: c["state"]
        for c in client.get("/candidates").json()
        if "rate limit" in c["content"]
    }
    assert len(states) == 2, f"both rate-limit facts should still exist: {states}"
    assert set(states.values()) == {"active"}, f"keep=all must keep both active: {states}"


def test_resolve_keep_none_rejects_all_and_clears_pending(client):
    # H11: keep="none" — reject every member of the cluster.
    pair = _surface_rate_limit_pair(client)

    res = client.post(f"/contradictions/{pair['id']}/resolve", json={"keep": "none"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["kept"] == []
    assert {f["state"] for f in body["rejected"]} == {"rejected"}

    assert client.get("/contradictions").json() == []
    # Neither rate-limit fact is in active recall any more.
    ctx = client.get("/context", params={"query": "rate limit"}).json()
    assert "100 requests per second" not in ctx["context"]
    assert "500 requests per second" not in ctx["context"]


def test_resolve_keep_subset_of_three(client):
    # H11: a 3-way contradiction (three mutually-conflicting numeric facts on the
    # same slot). Surface all three, then keep two and reject the third in one call.
    client.post("/insights", json={"insight": "The factory's default rate limit is 100 requests per second."})
    client.post("/insights", json={"insight": "The factory's default rate limit is 500 requests per second.", "onConflict": "surface"})
    client.post("/insights", json={"insight": "The factory's default rate limit is 900 requests per second.", "onConflict": "surface"})

    # All three are mutually flagged (three pending pairwise edges). Address the
    # whole 3-member cluster in one resolve by joining the member ids.
    by_val = {
        next(v for v in ("100", "500", "900") if v in c["content"]): c["id"]
        for c in client.get("/candidates").json()
        if "rate limit" in c["content"]
    }
    assert set(by_val) == {"100", "500", "900"}, by_val
    cluster_id = "__".join(sorted(by_val.values()))
    keep_ids = [by_val["100"], by_val["500"]]
    drop_id = by_val["900"]

    res = client.post(f"/contradictions/{cluster_id}/resolve", json={"keep": keep_ids})
    assert res.status_code == 200, res.text
    body = res.json()
    assert {f["id"] for f in body["kept"]} == set(keep_ids)
    assert [f["id"] for f in body["rejected"]] == [drop_id]
    assert {f["state"] for f in body["kept"]} == {"active"}

    # Cluster fully settled — every pairwise edge cleared, nothing left pending.
    assert client.get("/contradictions").json() == []
    states = {c["id"]: c["state"] for c in client.get("/candidates").json()}
    assert states[drop_id] == "rejected"
    for kid in keep_ids:
        assert states[kid] == "active"


def test_resolve_rejects_bad_keep_id(client):
    pair = _surface_rate_limit_pair(client)
    res = client.post(
        f"/contradictions/{pair['id']}/resolve", json={"keep": ["nonexistent_id"]}
    )
    assert res.status_code == 400, res.text


def test_context_hits_include_provenance_keys(client):
    client.post("/insights", json={"insight": "use uv, not pip, in this repo"})
    ctx = client.get("/context", params={"query": "deps install tool"}).json()
    assert ctx["hits"], "expected at least one hit"
    for hit in ctx["hits"]:
        # Enriched provenance shape (null when absent, but keys always present).
        assert set(hit) >= {"id", "text", "score", "source", "scope", "category"}


def test_batch_ingest_happy_path(client):
    body = {
        "documents": [
            {"text": "We deploy on Fridays here.", "source": "handbook"},
            {"text": "Code review requires two approvals.", "source": "handbook"},
        ],
        "state": "active",
    }
    res = client.post("/ingest", json=body)
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["count"] == 2
    assert len(payload["results"]) == 2
    for r in payload["results"]:
        assert r["action"] == "ingested"
        assert "id" in r
    # The ingested facts are now retrievable as active knowledge.
    ctx = client.get("/context", params={"query": "when do we deploy?"}).json()
    assert "Friday" in ctx["context"] or "friday" in ctx["context"].lower()


def test_batch_ingest_rejects_empty_documents(client):
    assert client.post("/ingest", json={"documents": []}).status_code == 400
    assert client.post("/ingest", json={}).status_code == 400
    assert (
        client.post("/ingest", json={"documents": [{"text": "  "}]}).status_code == 400
    )

