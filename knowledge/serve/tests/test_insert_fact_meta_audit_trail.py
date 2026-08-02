"""``POST /candidates`` must not silently discard a caller-supplied ``meta.auditTrail``.

The raw-insert path is documented as persisting ``meta`` as a free-form object, and it is the
seam every snapshot-to-snapshot ticket move, split, and manual repair goes through. It used to
build the fact's meta as ``{**user_meta, "auditTrail": [created]}`` — so a move carried its
provenance in, and the fact arrived reading as if it had been authored fresh. Silent and lossy:
no error, and a plausible single "created" entry in place of the history.

These pin merge semantics instead: the caller's entries survive AND the backend still records
that a create happened, in both working memory and a snapshot target.
"""

from __future__ import annotations

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"
PROJECT = "proj"
SNAPSHOT = "prd-proj"

SUPPLIED = [
    {"action": "created", "timestamp": "2026-01-01T00:00:00Z", "actor": "human-gate",
     "provenance": "prd-old"},
    {"action": "promoted_to_active", "timestamp": "2026-01-02T00:00:00Z", "actor": "human-gate",
     "provenance": "prd-old"},
    {"action": "edited", "timestamp": "2026-01-03T00:00:00Z", "actor": "af-build",
     "provenance": "prd-old"},
]


@pytest.fixture
def client(request, monkeypatch):
    monkeypatch.setenv("PRAXIS_AUTH_DISABLED", "1")
    from fastapi.testclient import TestClient

    from knowledge.knowledge_graph.knowledge_graph_variants import postgres_vector_graph
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder
    from knowledge.serve.app import create_app
    from knowledge.serve.orgs_store import OrgsStore
    from knowledge.serve.spaces_store import SpacesStore

    monkeypatch.setattr(postgres_vector_graph, "OpenRouterEmbedder", FakeEmbedder)

    org = "test_" + request.node.name
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
    SpacesStore(conn).create_space(org, PROJECT, PROJECT)
    yield TestClient(create_app(conn), headers={"X-Praxis-Org": org})
    _clean()


def _h(snapshot: str = SNAPSHOT) -> dict[str, str]:
    return {"X-Praxis-Space": PROJECT, "X-Praxis-Snapshot": snapshot}


def _body(**meta) -> dict:
    return {
        "title": "TMP-1 move the ticket",
        "content": "The ticket body, moved between snapshots verbatim.",
        "category": "requirement",
        "meta": {"requirement_id": "TMP-1", "auditTrail": list(SUPPLIED), **meta},
    }


def _trail(meta: dict) -> list[dict]:
    return meta["auditTrail"]


def _supplied_survive(trail: list[dict]) -> None:
    """Every supplied entry is present, in order, unmodified."""
    assert trail[: len(SUPPLIED)] == SUPPLIED


def test_snapshot_insert_preserves_supplied_audit_trail(client):
    """The reported repro: insert into a snapshot, read back with the facts_by query."""
    res = client.post("/candidates", json=_body(), headers=_h())
    assert res.status_code == 200, res.text

    rows = client.get(
        "/facts/by", params={"category": "requirement", "state": "any"}, headers=_h()
    ).json()
    rows = rows if isinstance(rows, list) else rows.get("facts", rows)
    hit = [f for f in rows if (f.get("meta") or {}).get("requirement_id") == "TMP-1"]
    assert len(hit) == 1
    trail = _trail(hit[0]["meta"])
    _supplied_survive(trail)
    # ...and the move is still recorded: the backend appends its own entry rather than
    # replacing, so the fact carries both its history and the fact that it arrived here.
    assert len(trail) == len(SUPPLIED) + 1
    assert trail[-1]["action"] == "created"


def test_working_memory_insert_preserves_supplied_audit_trail(client):
    """Same guarantee with no (space, snapshot) target — the clobber was not snapshot-specific."""
    res = client.post("/candidates", json=_body())
    assert res.status_code == 200, res.text
    got = client.get(f"/candidates/{res.json()['id']}").json()
    _supplied_survive(_trail(got["meta"]))
    # The read model's top-level auditTrail projects the same merged trail.
    _supplied_survive(got["auditTrail"])


def test_insert_without_an_audit_trail_still_gets_the_created_entry(client):
    """The common case is unchanged: no supplied trail means exactly the backend's own entry."""
    body = _body()
    body["meta"].pop("auditTrail")
    res = client.post("/candidates", json=body, headers=_h())
    assert res.status_code == 200, res.text
    trail = _trail(client.get(f"/candidates/{res.json()['id']}", headers=_h()).json()["meta"])
    assert len(trail) == 1 and trail[0]["action"] == "created"


def test_a_malformed_supplied_audit_trail_is_ignored_not_crashed(client):
    """``meta`` is free-form, so a non-list (or junk-entry) trail must not 500 the insert; the
    backend falls back to authoring its own trail rather than trusting the shape."""
    for junk in ("not-a-list", 42, {"action": "created"}):
        body = _body()
        body["meta"]["auditTrail"] = junk
        res = client.post("/candidates", json=body, headers=_h())
        assert res.status_code == 200, res.text
        trail = _trail(client.get(f"/candidates/{res.json()['id']}", headers=_h()).json()["meta"])
        assert isinstance(trail, list)
        assert trail[-1]["action"] == "created"


def test_insert_preserves_every_other_meta_key_verbatim(client):
    """``title`` is the one key the facade still owns (it mirrors the required ``title`` field).
    Nothing else the caller sends may be rewritten — this is what makes a move faithful."""
    extras = {
        "build_state": "finished",
        "depends_on": ["TMP-0"],
        "acceptance": {"nested": ["structure", 1, True]},
        "surface": "checkout",
        "claim": "an unrelated reserved-looking key",
    }
    res = client.post("/candidates", json=_body(**extras), headers=_h())
    assert res.status_code == 200, res.text
    meta = client.get(f"/candidates/{res.json()['id']}", headers=_h()).json()["meta"]
    for key, value in extras.items():
        assert meta[key] == value
    assert meta["requirement_id"] == "TMP-1"
    assert meta["title"] == "TMP-1 move the ticket"


MIXED_SHAPES = [
    # Real entry shapes from appeal_engine's prd snapshot — heterogeneous on purpose.
    {"actor": "human-gate", "action": "created", "timestamp": "2026-01-01T00:00:00Z",
     "provenance": "prd-appeal_engine"},
    {"actor": "af-build/appeal_engine", "action": "edited", "timestamp": "2026-01-02T00:00:00Z",
     "provenance": "prd-appeal_engine"},
    {"actor": "af-intake-plan-perf-compaction", "action": "compacted",
     "timestamp": "2026-01-03T00:00:00Z", "provenance": "prd-appeal_engine",
     "note": "compacted 32 entries to first+last; removed entries were per-heartbeat "
             "af-build edit records with no distinct information"},
    # No provenance key at all, plus a key no reader knows about.
    {"actor": "praxis", "action": "moved", "timestamp": "2026-01-04T00:00:00Z",
     "fromSnapshot": "prd-old"},
]


def test_ui_and_mcp_reads_agree_with_what_is_stored(client):
    """The detection gap this closes: the clobber went unnoticed because there was no
    ordinary way to LOOK at provenance. Both read surfaces must return the SAME trail the
    write path stored — a future write-path regression then shows up as a visible
    discrepancy instead of silent loss.

    ``GET /candidates/{id}`` is what the dashboard detail panel renders; ``GET /facts/by``
    is what ``praxis_facts_by`` returns to an agent. Asserted on a SNAPSHOT-resident fact,
    which is where provenance actually matters (those are the ones moved and repaired).
    """
    body = _body()
    body["meta"]["auditTrail"] = list(MIXED_SHAPES)
    fid = client.post("/candidates", json=body, headers=_h()).json()["id"]

    ui = client.get(f"/candidates/{fid}", headers=_h()).json()
    rows = client.get(
        "/facts/by", params={"category": "requirement", "state": "any"}, headers=_h()
    ).json()["facts"]
    mcp = [f for f in rows if f["id"] == fid][0]

    stored = MIXED_SHAPES + [{"action": "created"}]  # the backend's own appended entry
    for trail in (ui["meta"]["auditTrail"], ui["auditTrail"], mcp["meta"]["auditTrail"]):
        assert len(trail) == len(stored)
        assert trail[: len(MIXED_SHAPES)] == MIXED_SHAPES  # entry for entry, keys and all
        assert trail[-1]["action"] == "created"
    # The two surfaces agree with each other, not merely with the expectation.
    assert ui["meta"]["auditTrail"] == mcp["meta"]["auditTrail"]


def test_provenance_projection_returns_the_full_trail_without_the_bodies(client):
    """``fields=provenance`` exists because an exhaustive read of a real plan snapshot
    returns ~1.2 MB of requirement text, which overruns an agent's context and makes
    "just read the trail" impractical. It must drop the bodies and NOTHING of the trail."""
    body = _body()
    body["meta"]["auditTrail"] = list(MIXED_SHAPES)
    body["content"] = "x" * 5000
    fid = client.post("/candidates", json=body, headers=_h()).json()["id"]

    params = {"category": "requirement", "state": "any", "fields": "provenance"}
    rows = client.get("/facts/by", params=params, headers=_h()).json()["facts"]
    row = [f for f in rows if f["id"] == fid][0]

    full = [f for f in client.get(
        "/facts/by", params={"category": "requirement", "state": "any"}, headers=_h()
    ).json()["facts"] if f["id"] == fid][0]
    assert row["auditTrail"] == full["meta"]["auditTrail"]  # identical, not summarized
    assert row["auditTrailCount"] == len(row["auditTrail"])
    assert row["requirement_id"] == "TMP-1"
    # The bodies are what the projection is for: they must be gone.
    assert "text" not in row and "meta" not in row


def test_provenance_projection_rejects_an_unknown_fields_value(client):
    """An unrecognized projection must fail loudly rather than silently returning the
    full payload the caller was trying to avoid."""
    res = client.get(
        "/facts/by",
        params={"category": "requirement", "state": "any", "fields": "audit"},
        headers=_h(),
    )
    assert res.status_code == 400
    assert "provenance" in res.json()["detail"]


def test_snapshot_load_carries_the_trail_into_the_working_memory_view(client):
    """The dashboard reads a snapshot's facts by LOADING the snapshot into working memory,
    so that copy is part of the UI's provenance path — a trail dropped there is invisible
    in exactly the same way the insert clobber was."""
    body = _body()
    body["meta"]["auditTrail"] = list(MIXED_SHAPES)
    client.post("/candidates", json=body, headers=_h())

    loaded = client.post(
        "/snapshots/load", json={"space": PROJECT, "snapshot": SNAPSHOT, "mode": "replace"}
    )
    assert loaded.status_code == 200, loaded.text

    # No state filter — the dashboard's own unfiltered list read (``/candidates`` spells
    # "every state" as an absent filter, unlike ``/facts/by``'s ``state=any``).
    rows = client.get("/candidates").json()
    rows = rows if isinstance(rows, list) else rows.get("candidates", rows)
    hit = [c for c in rows if (c.get("meta") or {}).get("requirement_id") == "TMP-1"]
    assert len(hit) == 1
    assert hit[0]["meta"]["auditTrail"][: len(MIXED_SHAPES)] == MIXED_SHAPES


def test_add_insight_path_also_preserves_a_supplied_audit_trail(client):
    """``POST /insights`` (praxis_add_insight) is the pipelined sibling of the raw insert; a
    ticket authored through it must carry its supplied provenance too."""
    res = client.post(
        "/insights",
        json={
            "insight": "The ticket body, moved through the pipelined path.",
            "category": "requirement",
            "source": "prd-old",
            "meta": {
                "requirement_id": "TMP-2",
                "build_state": "unclaimed",
                "auditTrail": list(SUPPLIED),
            },
        },
        headers=_h(),
    )
    assert res.status_code == 200, res.text
    rows = client.get(
        "/facts/by", params={"category": "requirement", "state": "any"}, headers=_h()
    ).json()
    rows = rows if isinstance(rows, list) else rows.get("facts", rows)
    hit = [f for f in rows if (f.get("meta") or {}).get("requirement_id") == "TMP-2"]
    assert len(hit) == 1
    _supplied_survive(_trail(hit[0]["meta"]))
