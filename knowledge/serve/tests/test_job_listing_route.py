"""GET /jobs (R26): live jobs ordered so attention-needing jobs sort above
jobs progressing normally, the same data ``praxis_list_jobs`` (the MCP tool)
retrieves. Seeds ``app.state.job_store`` directly — real job creation/dispatch
routes are separate later work (see ``box_service_store.JobStore``'s own
docstring), so this asserts only the R26 slice: given a mix of progressing and
attention-needing jobs, the listing puts every attention-needing one first.
"""

from __future__ import annotations

import time

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.box_service_models import Job, JobState  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"


@pytest.fixture
def env(unique_org):
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", USER)
    app = create_app(conn)
    client = TestClient(app, headers={"X-Praxis-Org": org})
    yield client, app.state.job_store, org
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    conn.close()


def _seed(store, org, *, state, **extra):
    job = store.create(project="demo", snapshot="prd-demo")
    job.org = org
    job.state = state
    for k, v in extra.items():
        setattr(job, k, v)
    return job


def test_attention_needing_jobs_sort_above_progressing(env):
    client, store, org = env
    now = time.time()

    progressing_running = _seed(
        store, org, state=JobState.RUNNING, claim_heartbeat_at=now
    )
    progressing_queued = _seed(store, org, state=JobState.QUEUED)
    attention_awaiting = _seed(store, org, state=JobState.AWAITING_HUMAN)
    attention_failed = _seed(store, org, state=JobState.FAILED, failure_reason="oops")
    attention_stale = _seed(
        store, org, state=JobState.RUNNING, claim_heartbeat_at=now - 1000
    )

    res = client.get("/jobs")
    assert res.status_code == 200, res.text
    jobs = res.json()["jobs"]
    assert len(jobs) == 5

    by_id = {j["id"]: j for j in jobs}
    assert by_id[attention_awaiting.id]["attentionNeeded"] is True
    assert by_id[attention_failed.id]["attentionNeeded"] is True
    assert by_id[attention_stale.id]["attentionNeeded"] is True
    assert by_id[progressing_running.id]["attentionNeeded"] is False
    assert by_id[progressing_queued.id]["attentionNeeded"] is False

    order = [j["id"] for j in jobs]
    attention_ids = {attention_awaiting.id, attention_failed.id, attention_stale.id}
    progressing_ids = {progressing_running.id, progressing_queued.id}
    last_attention_idx = max(order.index(i) for i in attention_ids)
    first_progressing_idx = min(order.index(i) for i in progressing_ids)
    assert last_attention_idx < first_progressing_idx


def test_jobs_are_org_scoped(env):
    client, store, org = env
    other_job = store.create(project="demo", snapshot="prd-demo")
    other_job.org = "some-other-org"

    res = client.get("/jobs")
    assert res.status_code == 200, res.text
    assert res.json()["jobs"] == []
