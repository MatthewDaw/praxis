"""Integration tests for the /jobs endpoints (R26): live jobs and their states,
ordered so attention-needing jobs sort above jobs progressing normally, plus
the per-job activity read. Requires Postgres (same skip gate as test_server.py
since ``create_app`` needs a real connection for its other routes).
"""

from __future__ import annotations

import os
import time

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.box_service_models import JobState  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None or not os.getenv("OPENROUTER_API_KEY"),
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret) AND OPENROUTER_API_KEY",
)

USER = "dev-user"


@pytest.fixture
def client(unique_org):
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", USER)
    app = create_app(conn)
    yield TestClient(app, headers={"X-Praxis-Org": org})
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    conn.close()


def test_jobs_endpoint_is_empty_for_a_fresh_org(client):
    assert client.get("/jobs").json() == {"jobs": []}


def test_jobs_endpoint_orders_attention_needing_above_progressing(client):
    org = client.headers["X-Praxis-Org"]
    store = client.app.state.job_store

    progressing = store.create(project="p", snapshot="s")
    progressing.org = org
    progressing.state = JobState.RUNNING
    progressing.claim_heartbeat_at = time.time()

    attention = store.create(project="p", snapshot="s")
    attention.org = org
    attention.state = JobState.FAILED

    payload = client.get("/jobs").json()
    ids = [j["id"] for j in payload["jobs"]]

    assert ids.index(attention.id) < ids.index(progressing.id)
    by_id = {j["id"]: j for j in payload["jobs"]}
    assert by_id[attention.id]["needsAttention"] is True
    assert by_id[progressing.id]["needsAttention"] is False


def test_jobs_endpoint_scopes_to_the_active_org(client):
    store = client.app.state.job_store
    other_org_job = store.create(project="p", snapshot="s")
    other_org_job.org = "some-other-org"

    assert client.get("/jobs").json() == {"jobs": []}


def test_job_activity_endpoint_reads_the_stored_tail(client):
    org = client.headers["X-Praxis-Org"]
    store = client.app.state.job_store
    tail_store = client.app.state.activity_tail_store

    job = store.create(project="p", snapshot="s")
    job.org = org
    tail_store.append(job, "hello from the job")

    resp = client.get(f"/jobs/{job.id}/activity")

    assert resp.status_code == 200
    assert resp.json() == {"jobId": job.id, "activity": "hello from the job"}


def test_job_activity_endpoint_unknown_job_is_404(client):
    resp = client.get("/jobs/does-not-exist/activity")
    assert resp.status_code == 404


def test_job_activity_endpoint_scopes_to_the_active_org(client):
    store = client.app.state.job_store
    job = store.create(project="p", snapshot="s")
    job.org = "some-other-org"

    resp = client.get(f"/jobs/{job.id}/activity")

    assert resp.status_code == 404
