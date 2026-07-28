"""HTTP tests for GET /productivity (R3): owner-gated, four bucketed series.

Covers the ticket's acceptance condition directly: an authenticated token-owner
request with a valid range gets 200 with four named series (each an array of
``bucket_start``/``value`` pairs); an unauthenticated request gets 401; and a
non-owner principal — including one authenticated by an org API key — gets 403
with no git-derived number anywhere in the body.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.serve import apikeys, db  # noqa: E402
from knowledge.serve import productivity_cache, productivity_route  # noqa: E402
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


@pytest.fixture
def multi_org_ctx(unique_org, monkeypatch):
    """A user who belongs to TWO orgs, with every finished ticket in the NON-active one.

    A dedicated dev principal (``PRAXIS_DEV_USER_SUB``) rather than the shared
    ``dev-user``, so ``OrgsStore.list_orgs`` returns exactly these two orgs and the
    assertions can be exact set equalities.
    """
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
    productivity_cache.clear()

    user = unique_org + "_user"
    monkeypatch.setenv("PRAXIS_DEV_USER_SUB", user)

    db.bootstrap()
    conn = db.connect()
    active_org, other_org = unique_org + "_active", unique_org + "_other"
    orgs = [active_org, other_org]

    def _cleanup():
        conn.execute("DELETE FROM snapshots WHERE org_id = ANY(%s)", (orgs,))
        conn.execute("DELETE FROM api_keys WHERE org_id = ANY(%s)", (orgs,))
        conn.execute("DELETE FROM org_members WHERE org_id = ANY(%s)", (orgs,))
        conn.execute("DELETE FROM orgs WHERE org_id = ANY(%s)", (orgs,))

    _cleanup()
    for org in orgs:
        OrgsStore(conn).create_org(org, org, "pw", user)

    # Two tickets finished a few hours ago -- both in the org that is NOT selected.
    recent = datetime.now(timezone.utc) - timedelta(hours=3)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO snapshots "
            "(id, org_id, text, space, snapshot, state, category, meta) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            [
                (f"t-{i}", other_org, f"ticket {i}", "space-a", "prd-space-a", "active",
                 "requirement",
                 '{"build_state": "finished", "finished_at": "'
                 + (recent + timedelta(minutes=i)).isoformat() + '"}')
                for i in (1, 2)
            ],
        )

    client = TestClient(create_app(conn))
    yield {"client": client, "conn": conn, "active_org": active_org, "other_org": other_org,
           "user": user}
    _cleanup()
    productivity_cache.clear()
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

    # Per-repo breakdown: one entry per repo FAKE_ACTIVITY returned data for, same
    # owner-attributed totals as the aggregate above since there's only one repo.
    by_repo = body["series_by_repo"]
    assert set(by_repo.keys()) == {"acme/repo"}
    repo_series = by_repo["acme/repo"]
    assert set(repo_series.keys()) == {"s1_lines_added", "s2_lines_deleted", "s3_net_lines"}
    assert sum(p["value"] for p in repo_series["s1_lines_added"]) == 12
    assert sum(p["value"] for p in repo_series["s2_lines_deleted"]) == 4
    assert sum(p["value"] for p in repo_series["s3_net_lines"]) == 8


def test_s4_spans_every_org_the_user_belongs_to_not_just_the_active_one(
    multi_org_ctx, monkeypatch
):
    """The reported bug, end to end: the user belongs to two orgs and the tickets they
    finished today landed in the org that is NOT selected by ``X-Praxis-Org``. The
    aggregate S4 must still count them (it read as "no tickets completed" before), and
    ``series_by_org`` must attribute them to the org they actually live in."""
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)
    client = multi_org_ctx["client"]
    active_org, other_org = multi_org_ctx["active_org"], multi_org_ctx["other_org"]

    res = client.get(
        "/productivity", params={"range": "week"}, headers={"X-Praxis-Org": active_org}
    )
    assert res.status_code == 200, res.text
    body = res.json()

    # The active org holds ZERO finished tickets; both completions are in the other org.
    assert sum(p["value"] for p in body["series"]["s4_tickets_completed"]) == 2

    by_org = body["series_by_org"]
    # Every org the user belongs to gets an entry -- including the all-zero active one.
    assert set(by_org.keys()) == {active_org, other_org}
    assert sum(p["value"] for p in by_org[active_org]["s4_tickets_completed"]) == 0
    assert sum(p["value"] for p in by_org[other_org]["s4_tickets_completed"]) == 2
    assert by_org[other_org]["name"] == other_org

    # The per-org breakdown sums position-wise to the aggregate.
    aggregate = [p["value"] for p in body["series"]["s4_tickets_completed"]]
    per_org = [[p["value"] for p in v["s4_tickets_completed"]] for v in by_org.values()]
    assert [sum(vals) for vals in zip(*per_org)] == aggregate

    # ...and the instrumentation date comes from the non-active org's earliest finish.
    assert body["s4_instrumentation_date"] is not None

    # Every point in every per-org series has the same shape as ``series``' points.
    for entry in by_org.values():
        assert set(entry) == {"name", "s4_tickets_completed"}
        assert len(entry["s4_tickets_completed"]) == 7  # range=week
        for point in entry["s4_tickets_completed"]:
            assert set(point) == {"bucket_start", "value"}


def test_series_by_org_is_empty_when_the_ticket_series_fails(multi_org_ctx, monkeypatch):
    """Error isolation covers the per-org breakdown too: a failed S4 must never be
    reported as an all-zero per-org breakdown (indistinguishable from real inactivity)."""
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)

    def _boom(*_a, **_k):
        raise RuntimeError("boom: ticket store unavailable")

    monkeypatch.setattr(productivity_route, "s4_series", _boom)
    client, active_org = multi_org_ctx["client"], multi_org_ctx["active_org"]
    res = client.get(
        "/productivity", params={"range": "week"}, headers={"X-Praxis-Org": active_org}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["series_by_org"] == {}
    assert body["s4_instrumentation_date"] is None
    assert body["errors"]["s4_tickets_completed"]["reason"]
    # S1-S3 are unaffected.
    assert sum(p["value"] for p in body["series"]["s1_lines_added"]) == 12


def test_series_by_repo_omits_a_repo_with_no_commit_data(ctx, monkeypatch):
    """A repo with an empty commit list (e.g. genuinely inactive this window) still
    keys into ``series_by_repo`` with all-zero series -- only a repo the fetch
    OMITTED entirely (failed history fetch) is absent, never one with real zero data."""
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)
    monkeypatch.setattr(
        productivity_route.github_commits, "fetch_commit_activity",
        lambda *a, **k: {
            "repositories": {"acme/repo": [], "acme/other": FAKE_ACTIVITY["repositories"]["acme/repo"]},
            "truncated": False,
            "reason": None,
        },
    )
    client, org = ctx["client"], ctx["org"]
    res = client.get("/productivity", params={"range": "week"}, headers={"X-Praxis-Org": org})
    assert res.status_code == 200, res.text
    by_repo = res.json()["series_by_repo"]
    assert set(by_repo.keys()) == {"acme/repo", "acme/other"}
    assert sum(p["value"] for p in by_repo["acme/repo"]["s1_lines_added"]) == 0
    assert sum(p["value"] for p in by_repo["acme/other"]["s1_lines_added"]) == 12


def test_partial_failure_ticket_series_errors_git_series_still_renders(ctx, monkeypatch):
    """Ticket: when the ticket series (S4, Praxis-derived) errors and the git series
    (S1-S3, GitHub-derived) succeeds, S1-S3 must still render normally and the
    response must carry a per-series error for S4 naming the reason -- a failed
    series must never be indistinguishable from a genuine flat-zero line."""
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)

    def _boom(*_a, **_k):
        raise RuntimeError("boom: ticket store unavailable")

    monkeypatch.setattr(productivity_route, "s4_series", _boom)
    client, org = ctx["client"], ctx["org"]
    res = client.get("/productivity", params={"range": "week"}, headers={"X-Praxis-Org": org})
    assert res.status_code == 200, res.text
    body = res.json()
    series = body["series"]

    # S1-S3 (the git series) render normally, unaffected by the ticket-series failure.
    assert len(series["s1_lines_added"]) == 7
    assert sum(p["value"] for p in series["s1_lines_added"]) == 12
    assert sum(p["value"] for p in series["s2_lines_deleted"]) == 4
    assert sum(p["value"] for p in series["s3_net_lines"]) == 8

    # S4 (the ticket series) is empty rather than a confirmed zero, and the
    # response names the failure reason for that series specifically.
    assert series["s4_tickets_completed"] == []
    assert "errors" in body
    assert "s4_tickets_completed" in body["errors"]
    reason = body["errors"]["s4_tickets_completed"]["reason"]
    assert isinstance(reason, str) and reason


def test_invalid_range_is_400(ctx, monkeypatch):
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)
    client, org = ctx["client"], ctx["org"]
    res = client.get("/productivity", params={"range": "decade"}, headers={"X-Praxis-Org": org})
    assert res.status_code == 400, res.text


def test_invalid_bucket_unit_is_400(ctx, monkeypatch):
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)
    client, org = ctx["client"], ctx["org"]
    res = client.get(
        "/productivity",
        params={"range": "week", "bucketUnit": "hour"},
        headers={"X-Praxis-Org": org},
    )
    assert res.status_code == 400, res.text


def test_omitted_bucket_unit_preserves_default_behavior_for_every_range(ctx, monkeypatch):
    """Regression: omitting ``bucketUnit`` must yield the exact same bucket_unit/count
    that the range produced before ``bucketUnit`` existed."""
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)
    client, org = ctx["client"], ctx["org"]
    expected = {
        "day": ("hour", 24),
        "week": ("day", 7),
        "4weeks": ("day", 28),
    }
    for range_, (unit, count) in expected.items():
        res = client.get("/productivity", params={"range": range_}, headers={"X-Praxis-Org": org})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["bucket_unit"] == unit
        assert len(body["series"]["s1_lines_added"]) == count


def test_bucket_unit_week_on_4weeks_overrides_default_daily_bucketing(ctx, monkeypatch):
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)
    client, org = ctx["client"], ctx["org"]
    res = client.get(
        "/productivity",
        params={"range": "4weeks", "bucketUnit": "week"},
        headers={"X-Praxis-Org": org},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["bucket_unit"] == "week"
    # 28 days / 7-day buckets covers the same span in ~4 weekly buckets.
    assert len(body["series"]["s1_lines_added"]) == 4
