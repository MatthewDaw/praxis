"""End-to-end test of the TRUE parallel batch path (real per-worker connections).

The ``client`` fixture in ``test_server.py`` injects a single explicit connection,
which routes the batch writer down its serial fallback. Here we build the app with
NO explicit connection (``create_app()``), so it opens a fresh connection per
worker thread and exercises the real parallel-decide / serial-commit path against
the live DB — including the same-batch reconciliation that keeps dedup intact.

Requires a Postgres DSN and OPENROUTER_API_KEY (the write path embeds for real).
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None or not os.getenv("OPENROUTER_API_KEY"),
    reason="needs a Postgres DSN AND OPENROUTER_API_KEY (the write path embeds for real)",
)

USER = "dev-user"
_TABLES = ("fact_edges", "facts", "cached_facts", "org_members", "orgs")


@pytest.fixture
def parallel_client(unique_org):
    """A TestClient over an app with NO explicit conn -> real per-worker connections."""
    db.bootstrap()
    setup = db.connect()
    org = unique_org
    for table in _TABLES:
        setup.execute(f"DELETE FROM {table} WHERE org_id = %s", (org,))
    OrgsStore(setup).create_org(org, org, "pw", USER)
    # No explicit connection: create_app resolves the DSN and hands the batch
    # writer make_worker_conn, so each worker decides on its own connection.
    app = create_app()
    client = TestClient(app, headers={"X-Praxis-Org": org})
    try:
        yield client, setup, org
    finally:
        for table in _TABLES:
            setup.execute(f"DELETE FROM {table} WHERE org_id = %s", (org,))
        setup.close()


def _fact_count(conn, org) -> int:
    return conn.execute(
        "SELECT count(*) FROM facts WHERE org_id = %s AND user_id = %s", (org, USER)
    ).fetchone()[0]


def test_parallel_batch_preserves_same_batch_dedup(parallel_client):
    client, setup, org = parallel_client
    dup = "The deploy pipeline runs on push to main."
    r = client.post("/insights/batch", json={"insights": [
        {"insight": dup},
        {"insight": "Staging mirrors prod but uses a seeded database."},
        {"insight": dup},  # exact same-batch duplicate of item 0
        {"insight": "Secrets are sourced from AWS Secrets Manager at boot."},
    ]})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert all(res["ok"] for res in results), results
    assert all(res["retrievable"] for res in results), results

    # The same-batch duplicate merged into the first rather than both landing —
    # this is the reconciliation pass doing its job under real parallelism.
    assert results[0]["id"] == results[2]["id"]
    assert results[2]["action"] == "merged"
    # Three distinct inputs -> three facts, despite four items and parallel decide.
    assert _fact_count(setup, org) == 3


def test_parallel_batch_distinct_items_all_land(parallel_client):
    client, setup, org = parallel_client
    insights = [{"insight": f"Service {i} owns the widget-{i} table exclusively."} for i in range(6)]
    r = client.post("/insights/batch", json={"insights": insights})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert all(res["ok"] and res["retrievable"] for res in results), results
    assert len({res["id"] for res in results}) == 6
    assert _fact_count(setup, org) == 6
