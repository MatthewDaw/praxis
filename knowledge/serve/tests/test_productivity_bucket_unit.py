"""Specs for R8: per-range bucket-unit selection and sum aggregation.

Covers the ticket acceptance condition directly:
  * range=day -> bucket_unit "hour", 24 buckets
  * range=week -> bucket_unit "day", 7 buckets
  * range=12months -> bucket_unit "week", and each weekly bucket SUMS the values of every day
    it contains rather than averaging them (10/day over 7 days -> 70 for the week).
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
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402
from knowledge.serve.productivity_attribution import bucketed_owner_totals  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret)",
)

OWNER_EMAIL = productivity_route.DEFAULT_OWNER_EMAIL
NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)


def test_bucket_plan_day_is_hourly():
    bucket_starts, seconds, _start, _end, unit = productivity_route.bucket_plan(
        productivity_route.DAY, NOW
    )
    assert unit == "hour"
    assert len(bucket_starts) == 24
    assert seconds == 3600.0


def test_bucket_plan_week_is_daily():
    bucket_starts, seconds, _start, _end, unit = productivity_route.bucket_plan(
        productivity_route.WEEK, NOW
    )
    assert unit == "day"
    assert len(bucket_starts) == 7
    assert seconds == 86400.0


def test_bucket_plan_four_weeks_is_daily():
    bucket_starts, _seconds, _start, _end, unit = productivity_route.bucket_plan(
        productivity_route.FOUR_WEEKS, NOW
    )
    assert unit == "day"
    assert len(bucket_starts) == 28


def test_bucket_plan_twelve_months_is_weekly():
    bucket_starts, seconds, _start, _end, unit = productivity_route.bucket_plan(
        productivity_route.TWELVE_MONTHS, NOW
    )
    assert unit == "week"
    assert seconds == 7 * 86400.0
    assert len(bucket_starts) >= 48  # ~52 weeks in 12 months


def test_bucket_plan_alltime_is_monthly():
    bucket_starts, _seconds, _start, _end, unit = productivity_route.bucket_plan(
        productivity_route.ALLTIME, NOW
    )
    assert unit == "month"
    assert len(bucket_starts) >= 1


def test_twelve_months_weekly_bucket_sums_not_averages_daily_activity():
    """10 lines added on each of 7 consecutive days lands in one weekly bucket and SUMS to 70,
    never averaging down to a per-day figure."""
    bucket_starts, seconds, _start, _end, unit = productivity_route.bucket_plan(
        productivity_route.TWELVE_MONTHS, NOW
    )
    assert unit == "week"
    target_bucket_start = bucket_starts[-1]
    commits = [
        {
            "additions": 10,
            "deletions": 0,
            "committedDate": (target_bucket_start + timedelta(days=i)).isoformat().replace(
                "+00:00", "Z"
            ),
            "author_login": productivity_route.DEFAULT_OWNER_LOGIN,
        }
        for i in range(7)
    ]
    totals = bucketed_owner_totals(
        {"acme/repo": commits},
        productivity_route.DEFAULT_OWNER_LOGIN,
        bucket_starts,
        seconds,
        owner_emails=[OWNER_EMAIL],
    )
    assert totals["s1"][-1] == 70


@pytest.fixture
def ctx(unique_org, monkeypatch):
    monkeypatch.setattr(
        productivity_route.github_commits, "fetch_commit_activity",
        lambda *a, **k: {"repositories": {}, "truncated": False, "reason": None},
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
    conn.execute("DELETE FROM api_keys WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", "dev-user")
    app = create_app(conn)
    client = TestClient(app)
    yield {"client": client, "conn": conn, "org": org}
    conn.execute("DELETE FROM api_keys WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    conn.close()


def test_route_response_carries_bucket_unit_for_day(ctx, monkeypatch):
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)
    client, org = ctx["client"], ctx["org"]
    res = client.get("/productivity", params={"range": "day"}, headers={"X-Praxis-Org": org})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["bucket_unit"] == "hour"
    assert len(body["series"]["s1_lines_added"]) == 24


def test_route_response_carries_bucket_unit_for_week(ctx, monkeypatch):
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)
    client, org = ctx["client"], ctx["org"]
    res = client.get("/productivity", params={"range": "week"}, headers={"X-Praxis-Org": org})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["bucket_unit"] == "day"
    assert len(body["series"]["s1_lines_added"]) == 7
