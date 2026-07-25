"""HTTP tests for GET /productivity (R3): owner-gated, four bucketed series.

Covers the ticket's acceptance condition directly: an authenticated token-owner
request with a valid range gets 200 with four named series (each an array of
``bucket_start``/``value`` pairs); an unauthenticated request gets 401; and a
non-owner principal — including one authenticated by an org API key — gets 403
with no git-derived number anywhere in the body.
"""

from __future__ import annotations

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.serve import apikeys, db  # noqa: E402
from knowledge.serve import productivity_route  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret)",
)

OWNER_EMAIL = productivity_route.DEFAULT_OWNER_EMAIL

FAKE_ACTIVITY = {
    "repositories": {
        "acme/repo": [
            {
                "additions": 12,
                "deletions": 4,
                "committedDate": "2026-07-24T10:00:00Z",
                "author_login": productivity_route.DEFAULT_OWNER_LOGIN,
            },
            {
                "additions": 999,
                "deletions": 999,
                "committedDate": "2026-07-24T11:00:00Z",
                "author_login": "someone-else",
            },
        ]
    },
    "truncated": False,
    "reason": None,
}


def _seed_org(conn, org, user="dev-user"):
    conn.execute("DELETE FROM api_keys WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", user)


@pytest.fixture
def ctx(unique_org, monkeypatch):
    monkeypatch.setattr(
        productivity_route.github_commits, "fetch_commit_activity",
        lambda *a, **k: dict(FAKE_ACTIVITY),
    )
    monkeypatch.setattr(
        productivity_route.github_token, "resolve_github_token", lambda: "fake-token"
    )
    monkeypatch.setattr(
        productivity_route.github_audit, "record_github_use", lambda *a, **k: None
    )

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


def test_unauthenticated_request_is_401(ctx, monkeypatch):
    monkeypatch.delenv("PRAXIS_AUTH_DISABLED", raising=False)
    client, org = ctx["client"], ctx["org"]
    res = client.get("/productivity", params={"range": "week"}, headers={"X-Praxis-Org": org})
    assert res.status_code == 401, res.text


def test_non_owner_principal_gets_403_with_no_git_numbers(ctx, monkeypatch):
    # Dev seam defaults to sub="dev-user" email="dev@local" -- not the configured owner.
    monkeypatch.delenv("PRAXIS_DEV_USER_EMAIL", raising=False)
    monkeypatch.delenv("PRODUCTIVITY_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("PRODUCTIVITY_OWNER_EMAILS", raising=False)
    client, org = ctx["client"], ctx["org"]
    res = client.get("/productivity", params={"range": "week"}, headers={"X-Praxis-Org": org})
    assert res.status_code == 403, res.text
    assert "12" not in res.text and "s1_lines_added" not in res.text


def test_api_key_principal_gets_403(ctx, monkeypatch):
    monkeypatch.delenv("PRODUCTIVITY_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("PRODUCTIVITY_OWNER_EMAILS", raising=False)
    client, org, conn = ctx["client"], ctx["org"], ctx["conn"]
    _key_id, raw_key = apikeys.mint_key(conn, org, user_id="dev-user", label="ci")
    res = client.get(
        "/productivity",
        params={"range": "week"},
        headers={"X-Praxis-Org": org, "X-Praxis-Key": raw_key},
    )
    assert res.status_code == 403, res.text
    assert "s1_lines_added" not in res.text


def test_owner_authenticated_request_returns_four_series(ctx, monkeypatch):
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)
    client, org = ctx["client"], ctx["org"]
    res = client.get("/productivity", params={"range": "week"}, headers={"X-Praxis-Org": org})
    assert res.status_code == 200, res.text
    body = res.json()
    series = body["series"]
    for key in (
        "s1_lines_added", "s2_lines_deleted", "s3_net_lines", "s4_tickets_completed",
    ):
        assert key in series
        assert isinstance(series[key], list) and len(series[key]) == 7  # range=week
        for point in series[key]:
            assert set(point) == {"bucket_start", "value"}

    # The owner's commit (additions=12, deletions=4) is attributed; the other
    # author's (additions=999) never counts toward the owner's series.
    assert sum(p["value"] for p in series["s1_lines_added"]) == 12
    assert sum(p["value"] for p in series["s2_lines_deleted"]) == 4
    assert sum(p["value"] for p in series["s3_net_lines"]) == 8


def test_invalid_range_is_400(ctx, monkeypatch):
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)
    client, org = ctx["client"], ctx["org"]
    res = client.get("/productivity", params={"range": "decade"}, headers={"X-Praxis-Org": org})
    assert res.status_code == 400, res.text
