"""HTTP tests for the productivity kill switch (R39).

Covers the ticket's acceptance condition directly: given the kill switch is
set, ``GET /productivity`` returns a disabled status (not an error), the route
never issues a GitHub call, and the disabled signal is present regardless of
whether the caller is the token owner.
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


def _seed_org(conn, org, user="dev-user"):
    conn.execute("DELETE FROM api_keys WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", user)


@pytest.fixture
def ctx(unique_org, monkeypatch):
    calls = {"github": 0}

    def _fake_fetch(*_a, **_k):
        calls["github"] += 1
        return {"repositories": {}, "truncated": False, "reason": None}

    monkeypatch.setattr(productivity_route.github_commits, "fetch_commit_activity", _fake_fetch)
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
    yield {"client": client, "conn": conn, "org": org, "calls": calls}
    conn.execute("DELETE FROM api_keys WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    conn.close()


def test_kill_switch_disables_route_for_owner_with_no_github_call(ctx, monkeypatch):
    monkeypatch.setenv("PRODUCTIVITY_KILL_SWITCH", "1")
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)
    client, org, calls = ctx["client"], ctx["org"], ctx["calls"]
    res = client.get("/productivity", params={"range": "week"}, headers={"X-Praxis-Org": org})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body.get("status") == "disabled"
    assert "series" not in body
    assert calls["github"] == 0


def test_kill_switch_disables_route_for_non_owner_with_no_github_call(ctx, monkeypatch):
    monkeypatch.setenv("PRODUCTIVITY_KILL_SWITCH", "true")
    monkeypatch.delenv("PRAXIS_DEV_USER_EMAIL", raising=False)
    monkeypatch.delenv("PRODUCTIVITY_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("PRODUCTIVITY_OWNER_EMAILS", raising=False)
    client, org, calls = ctx["client"], ctx["org"], ctx["calls"]
    res = client.get("/productivity", params={"range": "week"}, headers={"X-Praxis-Org": org})
    assert res.status_code == 200, res.text
    assert res.json().get("status") == "disabled"
    assert calls["github"] == 0


def test_kill_switch_off_behaves_as_before(ctx, monkeypatch):
    monkeypatch.delenv("PRODUCTIVITY_KILL_SWITCH", raising=False)
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)
    client, org, calls = ctx["client"], ctx["org"], ctx["calls"]
    res = client.get("/productivity", params={"range": "week"}, headers={"X-Praxis-Org": org})
    assert res.status_code == 200, res.text
    assert "series" in res.json()
    assert calls["github"] == 1
