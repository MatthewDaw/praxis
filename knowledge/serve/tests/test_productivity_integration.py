"""End-to-end integration test for GET /productivity (R30).

Exercises the real route through the FULL stack down to a MOCKED GitHub GraphQL
*transport* (the ``(query, variables, token) -> response`` seam in
``github_commits.py`` — not a stand-in for ``fetch_commit_activity`` itself, so the
real history query construction, response parsing and retry/truncation logic all
genuinely run) and a Praxis ``snapshots`` table SEEDED with a real finished
ticket, so the S4 series is exercised against real seeded data rather than an
empty table.

Acceptance condition (R30): the integration test asserts all four series values,
a truncated case and a non-owner 403 case, and fails if any of those assertions
regresses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.serve import db  # noqa: E402
from knowledge.serve import productivity_route  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.github_commits import GitHubTimeout  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret)",
)

OWNER_EMAIL = productivity_route.DEFAULT_OWNER_EMAIL
OWNER_LOGIN = productivity_route.DEFAULT_OWNER_LOGIN
REPO = "acme/repo"


def _history_response(commits: list[dict]) -> dict:
    return {
        "data": {
            "repository": {
                "defaultBranchRef": {
                    "target": {
                        "history": {
                            "nodes": [
                                {
                                    "additions": c["additions"],
                                    "deletions": c["deletions"],
                                    "committedDate": c["committedDate"],
                                    "author": {"user": {"login": c["author_login"]}},
                                }
                                for c in commits
                            ]
                        }
                    }
                }
            },
            "rateLimit": {"cost": 1},
        }
    }


def _seed_org(conn, org, user="dev-user"):
    conn.execute("DELETE FROM api_keys WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM snapshots WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", user)


def _seed_finished_ticket(conn, org, ticket_id, finished_at: datetime):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO snapshots "
            "(id, org_id, text, space, snapshot, state, category, meta) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            (
                ticket_id, org, "a seeded finished ticket",
                "productivity-eval", "prd-productivity-eval", "active", "requirement",
                f'{{"finished_at": "{finished_at.isoformat()}"}}',
            ),
        )


@pytest.fixture
def ctx(unique_org, monkeypatch):
    monkeypatch.setattr(
        productivity_route.github_token, "resolve_github_token", lambda: "fake-token"
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
    conn.execute("DELETE FROM snapshots WHERE org_id = %s", (org,))
    conn.close()


def test_owner_request_returns_four_series_from_the_real_graphql_parse_path_and_seeded_tickets(
    ctx, monkeypatch
):
    """An owner-authenticated request goes through the real query/parse/attribution
    path (only the transport is faked) and the S4 series reflects a REAL seeded
    Praxis ticket, not an empty table."""
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)
    monkeypatch.setenv("PRODUCTIVITY_TRACKED_REPOS", REPO)
    client, org, conn = ctx["client"], ctx["org"], ctx["conn"]

    now = datetime.now(timezone.utc)
    owner_commit_at = (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    other_commit_at = (now - timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
    commits = [
        {"additions": 12, "deletions": 4, "committedDate": owner_commit_at, "author_login": OWNER_LOGIN},
        {"additions": 999, "deletions": 999, "committedDate": other_commit_at, "author_login": "someone-else"},
    ]

    calls: list[str] = []

    def fake_transport(query, variables, token):
        assert token == "fake-token"
        calls.append("history")
        assert variables["owner"] == "acme" and variables["name"] == "repo"
        return _history_response(commits)

    monkeypatch.setattr(
        productivity_route.github_commits, "_default_transport", fake_transport
    )

    _seed_finished_ticket(conn, org, f"{org}-ticket-1", now - timedelta(minutes=10))
    _seed_finished_ticket(conn, org, f"{org}-ticket-2", now - timedelta(minutes=5))

    res = client.get("/productivity", params={"range": "day"}, headers={"X-Praxis-Org": org})
    assert res.status_code == 200, res.text
    body = res.json()

    assert calls == ["history"]
    assert body["truncated"] is False

    series = body["series"]
    for key in ("s1_lines_added", "s2_lines_deleted", "s3_net_lines", "s4_tickets_completed"):
        assert key in series
        assert isinstance(series[key], list) and len(series[key]) == 24  # range=day -> 24 hourly buckets
        for point in series[key]:
            assert set(point) == {"bucket_start", "value"}

    # Only the owner's commit (additions=12, deletions=4) is attributed; the
    # other author's (additions=999) never counts toward the owner's series.
    assert sum(p["value"] for p in series["s1_lines_added"]) == 12
    assert sum(p["value"] for p in series["s2_lines_deleted"]) == 4
    assert sum(p["value"] for p in series["s3_net_lines"]) == 8
    # Both seeded finished tickets fall inside the 24h window.
    assert sum(p["value"] for p in series["s4_tickets_completed"]) == 2


def test_github_failure_surfaces_as_truncated_true_never_a_confident_zero(ctx, monkeypatch):
    """A GitHub transport that never succeeds (bounded retries exhausted) is
    surfaced as ``truncated: true`` rather than a silent, confident zero-activity
    response (R37's acceptance condition, exercised end to end through the route)."""
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)
    # Pin to a single repo (rather than the 5-repo default) so this test's exhausted-retries
    # backoff sleeps only once per-repo's worth of real time, not once per default repo.
    monkeypatch.setenv("PRODUCTIVITY_TRACKED_REPOS", REPO)
    client, org = ctx["client"], ctx["org"]

    def always_times_out(query, variables, token):
        raise GitHubTimeout("simulated timeout")

    monkeypatch.setattr(
        productivity_route.github_commits, "_default_transport", always_times_out
    )

    res = client.get("/productivity", params={"range": "day"}, headers={"X-Praxis-Org": org})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["truncated"] is True
    # Every series is still well-formed (empty of GitHub data, not an error).
    for key in ("s1_lines_added", "s2_lines_deleted", "s3_net_lines"):
        assert sum(p["value"] for p in body["series"][key]) == 0


def test_non_owner_principal_gets_403_and_never_reaches_github(ctx, monkeypatch):
    """A non-owner principal is refused before any GitHub call is made and before
    any git-derived number reaches the response body."""
    monkeypatch.delenv("PRAXIS_DEV_USER_EMAIL", raising=False)
    monkeypatch.delenv("PRODUCTIVITY_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("PRODUCTIVITY_OWNER_EMAILS", raising=False)
    client, org = ctx["client"], ctx["org"]

    def must_not_be_called(query, variables, token):
        raise AssertionError("GitHub transport must never be called for a non-owner principal")

    monkeypatch.setattr(
        productivity_route.github_commits, "_default_transport", must_not_be_called
    )

    res = client.get("/productivity", params={"range": "day"}, headers={"X-Praxis-Org": org})
    assert res.status_code == 403, res.text
    assert "12" not in res.text and "s1_lines_added" not in res.text
