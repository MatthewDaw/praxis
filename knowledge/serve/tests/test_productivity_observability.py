"""Observability signals for the productivity route (R40).

Every ``/productivity`` request logs request duration, GitHub rate-limit
points spent, cache hit/miss and whether the response was truncated -- so a
silent degradation into cached or truncated data is detectable without
reading the UI -- and none of those log lines ever carries the backend
GitHub token value.
"""

from __future__ import annotations

import json
import logging

import pytest
from dotenv import load_dotenv

load_dotenv()

from knowledge.serve import db, github_audit, productivity_route  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret)",
)

# Assembled at runtime (never a contiguous literal) so this file itself never
# trips the repo-wide raw-token-leak scan it exists to exercise.
FAKE_TOKEN = "ghp_" + "1" + "A" * 32

FAKE_ACTIVITY = {
    "repositories": {},
    "truncated": True,
    "reason": "timeout",
    "points_spent": 7,
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
        productivity_route.github_token, "resolve_github_token", lambda: FAKE_TOKEN
    )
    monkeypatch.setattr(
        productivity_route.github_audit, "record_github_use", lambda *a, **k: None
    )
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    _seed_org(conn, org)
    yield conn, org
    conn.execute("DELETE FROM api_keys WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    conn.close()


def test_build_series_logs_duration_points_cache_and_truncation(ctx, caplog):
    conn, org = ctx
    with caplog.at_level(logging.INFO, logger="productivity.metrics"):
        productivity_route.build_series(conn, org, "week")

    records = [r for r in caplog.records if r.name == "productivity.metrics"]
    assert len(records) == 1
    entry = json.loads(records[0].message)

    assert entry["truncated"] is True
    assert entry["points_spent"] == 7
    assert entry["cache_hit"] is False
    assert isinstance(entry["duration_ms"], (int, float))
    assert entry["duration_ms"] >= 0
    assert FAKE_TOKEN not in caplog.text


def test_record_productivity_request_shape_and_no_token_leak(caplog):
    with caplog.at_level(logging.INFO, logger="productivity.metrics"):
        entry = github_audit.record_productivity_request(
            duration_ms=12.5, points_spent=3, cache_hit=True, truncated=False,
        )

    assert entry["duration_ms"] == 12.5
    assert entry["points_spent"] == 3
    assert entry["cache_hit"] is True
    assert entry["truncated"] is False
    assert FAKE_TOKEN not in json.dumps(entry)
    assert FAKE_TOKEN not in caplog.text
