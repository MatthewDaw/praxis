"""GET /productivity reports the first-run signal (R20).

A user with zero discovered GitHub repositories and zero Praxis spaces gets
``repos_discovered: 0`` / ``spaces_count: 0`` in the response so the client can
render a dedicated first-run message instead of a chart whose every series
happens to be a flat zero line -- indistinguishable from "connected but did no
work" without this signal.
"""

from __future__ import annotations

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.serve import db  # noqa: E402
from knowledge.serve import productivity_route  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret)",
)

OWNER_EMAIL = productivity_route.DEFAULT_OWNER_EMAIL

EMPTY_ACTIVITY = {"repositories": {}, "truncated": False, "reason": None}


def _seed_org(conn, org, user="dev-user"):
    conn.execute("DELETE FROM api_keys WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", user)


@pytest.fixture
def ctx(unique_org, monkeypatch):
    monkeypatch.setattr(
        productivity_route.github_commits, "fetch_commit_activity",
        lambda *a, **k: dict(EMPTY_ACTIVITY),
    )
    monkeypatch.setattr(
        productivity_route.github_token, "resolve_github_token", lambda: "fake-token"
    )
    monkeypatch.setattr(
        productivity_route.github_audit, "record_github_use", lambda *a, **k: None
    )
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)

    db.bootstrap()
    conn = db.connect()
    org = unique_org
    _seed_org(conn, org)
    app = create_app(conn)
    client = TestClient(app)
    yield {"client": client, "conn": conn, "org": org}
    conn.execute("DELETE FROM api_keys WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    conn.close()


def test_zero_repos_and_zero_spaces_reports_first_run_counts(ctx):
    client, org = ctx["client"], ctx["org"]
    # No spaces have been created for this org (org has zero Praxis spaces),
    # and the (mocked) GitHub discovery found zero active repositories.
    res = client.get("/productivity", params={"range": "week"}, headers={"X-Praxis-Org": org})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["repos_discovered"] == 0
    assert body["spaces_count"] == 0


def test_a_connected_space_counts_toward_spaces_count(ctx):
    client, conn, org = ctx["client"], ctx["conn"], ctx["org"]
    conn.execute(
        "INSERT INTO spaces (org_id, space_id, name) VALUES (%s, %s, %s)",
        (org, "space-1", "Space One"),
    )
    res = client.get("/productivity", params={"range": "week"}, headers={"X-Praxis-Org": org})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["repos_discovered"] == 0
    assert body["spaces_count"] == 1
