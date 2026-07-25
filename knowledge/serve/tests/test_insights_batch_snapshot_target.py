"""POST /insights/batch honors X-Praxis-Space / X-Praxis-Snapshot.

Regression for a SILENT data-loss bug: the batch route built its write graph as
``PostgresVectorGraph(conn, org, uid, ...)`` directly instead of going through
``graph_for(org, uid, target, ...)``, so the ``(space, snapshot)`` headers were
parsed by the ``snapshot_target`` dependency on the singular route and simply
dropped here. A caller batching facts at an org-shared snapshot got HTTP 200 with
``ok``/``action="added"``/``retrievable=True`` for every item while all of them
landed in the requester's private working memory. The only way to notice was to
query the snapshot afterwards and find it empty (this happened for real with a
75-fact batch).

So the assertions below are deliberately two-sided: the facts must be IN the
targeted snapshot AND NOT in working memory. Before the fix the first half fails
and the second half fails too.

No OPENROUTER_API_KEY needed: the fixture swaps the default embedder for the
deterministic ``FakeEmbedder`` and the batches use ``raw=True``, the redact-only
fast lane that skips the Deduper + LLM conflict steps.
"""

from __future__ import annotations

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"  # the PRAXIS_AUTH_DISABLED dev principal sub
PROJECT = "batchproj"
SNAPSHOT = f"prd-{PROJECT}"
SOURCE = SNAPSHOT
TEXTS = [
    "the batch route must write to the targeted snapshot",
    "working memory must stay empty when a snapshot is targeted",
    "a silent 200 is worse than a loud 400",
]


@pytest.fixture
def env(unique_org, monkeypatch):
    """TestClient over ``create_app(conn)`` plus a registered space to target.

    The server constructs its own graphs (no fakes injected by ``create_app``), so
    the module-level default embedder is swapped for ``FakeEmbedder`` — that is the
    seam that keeps this test offline while still exercising the real HTTP write path.
    """
    monkeypatch.setenv("PRAXIS_AUTH_DISABLED", "1")
    from fastapi.testclient import TestClient

    from knowledge.knowledge_graph.knowledge_graph_variants import postgres_vector_graph
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder
    from knowledge.serve.app import create_app
    from knowledge.serve.orgs_store import OrgsStore
    from knowledge.serve.spaces_store import SpacesStore

    monkeypatch.setattr(postgres_vector_graph, "OpenRouterEmbedder", FakeEmbedder)

    org = unique_org
    tables = (
        "fact_edges", "facts", "snapshot_edges", "snapshots",
        "org_members", "orgs", "spaces",
    )

    db.bootstrap()
    conn = db.connect()

    def _clean() -> None:
        for table in tables:
            conn.execute(f"DELETE FROM {table} WHERE org_id = %s", (org,))

    _clean()
    OrgsStore(conn).create_org(org, org, "pw", USER)
    SpacesStore(conn).create_space(org, PROJECT, PROJECT)

    client = TestClient(create_app(conn), headers={"X-Praxis-Org": org})
    yield client, conn, org
    _clean()
    conn.close()


def _headers(space: str = PROJECT, snapshot: str = SNAPSHOT) -> dict[str, str]:
    return {"X-Praxis-Space": space, "X-Praxis-Snapshot": snapshot}


def _batch(client, texts, headers=None, **body):
    return client.post(
        "/insights/batch",
        json={
            "insights": [{"insight": t, "source": SOURCE} for t in texts],
            "raw": True,
            **body,
        },
        headers=headers or {},
    )


def _snapshot_texts(conn, org) -> set[str]:
    rows = conn.execute(
        "SELECT text FROM snapshots WHERE org_id = %s AND space = %s AND snapshot = %s",
        (org, PROJECT, SNAPSHOT),
    ).fetchall()
    return {r[0] for r in rows}


def _working_memory_texts(conn, org) -> set[str]:
    rows = conn.execute(
        "SELECT text FROM facts WHERE org_id = %s AND user_id = %s", (org, USER)
    ).fetchall()
    return {r[0] for r in rows}


def test_batch_with_space_snapshot_headers_lands_in_snapshot(env):
    """The headers route the WHOLE batch to the snapshot — and nothing to working memory."""
    client, conn, org = env
    res = _batch(client, TEXTS, _headers())
    assert res.status_code == 200, res.text
    results = res.json()["results"]
    assert res.json()["count"] == len(TEXTS)
    assert all(r["ok"] and r["action"] == "added" and r["retrievable"] for r in results), results

    # The claim the 200 made must be true of the SNAPSHOT, not working memory.
    assert _snapshot_texts(conn, org) == set(TEXTS)
    assert _working_memory_texts(conn, org) == set()

    # And the caller's own read-back path (GET /facts/by with the same headers,
    # i.e. the query that exposed the bug) sees them.
    read = client.get("/facts/by", params={"source": SOURCE}, headers=_headers())
    assert read.status_code == 200, read.text
    assert {f["text"] for f in read.json()["facts"]} == set(TEXTS)
    # The ids the batch reported are the snapshot's ids, not orphans.
    assert {r["id"] for r in results} == {f["id"] for f in read.json()["facts"]}


def test_batch_without_headers_still_writes_working_memory(env):
    """No headers (or only one) => working memory, exactly as before the fix."""
    client, conn, org = env
    assert _batch(client, TEXTS).status_code == 200
    assert _working_memory_texts(conn, org) == set(TEXTS)
    assert _snapshot_texts(conn, org) == set()

    # Only one of the pair is not a target: snapshot_target returns None, so this
    # falls back to working memory rather than erroring — matching POST /insights.
    partial = _batch(client, ["only the space header was supplied"], {"X-Praxis-Space": PROJECT})
    assert partial.status_code == 200, partial.text
    assert _snapshot_texts(conn, org) == set()


def test_batch_targeting_unknown_space_is_404(env):
    """An unknown space is a 404 from ``_require_space``, same as the singular route."""
    client, conn, org = env
    res = _batch(client, TEXTS, _headers(space="no-such-space", snapshot="prd-nope"))
    assert res.status_code == 404, res.text
    assert _working_memory_texts(conn, org) == set()  # nothing leaked on the way out


def test_bad_item_does_not_abort_the_snapshot_batch(env):
    """Per-item isolation is preserved on the snapshot path: one bad item, rest land."""
    client, conn, org = env
    res = client.post(
        "/insights/batch",
        json={
            "insights": [
                {"insight": TEXTS[0], "source": SOURCE},
                "not an object",
                {"insight": "   ", "source": SOURCE},
                {"insight": TEXTS[1], "source": SOURCE, "meta": "not an object"},
                {"insight": TEXTS[2], "source": SOURCE},
            ],
            "raw": True,
        },
        headers=_headers(),
    )
    assert res.status_code == 200, res.text
    results = res.json()["results"]
    assert [r["ok"] for r in results] == [True, False, False, False, True]
    assert results[1]["error"] == "each insight must be an object"
    assert results[2]["error"] == "insight required"
    assert results[3]["error"] == "meta must be an object"
    assert _snapshot_texts(conn, org) == {TEXTS[0], TEXTS[2]}
    assert _working_memory_texts(conn, org) == set()
