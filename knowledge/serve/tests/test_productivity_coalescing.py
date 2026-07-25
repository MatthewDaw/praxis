"""Concurrent-request coalescing for GET /productivity (single-flight fan-out).

Acceptance condition: given three simultaneous requests for the same range and
cache key, exactly one GitHub fan-out is issued and all three responses carry
the same ``computed_at``. Without coalescing, three concurrent misses each call
:func:`knowledge.serve.productivity_route.build_series` independently -- a
thundering herd from multiple tabs (or a forced refresh) that multiplies the
shared GitHub rate-limit spend by the number of concurrent callers.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv

load_dotenv()

from knowledge.serve import db, productivity_cache  # noqa: E402
from knowledge.serve import productivity_route  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret)",
)

FAKE_ACTIVITY = {
    "repositories": {
        "acme/repo": [
            {
                "additions": 12,
                "deletions": 4,
                "committedDate": "2026-07-24T10:00:00Z",
                "author_login": productivity_route.DEFAULT_OWNER_LOGIN,
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
    productivity_cache.clear()
    monkeypatch.setattr(
        productivity_route.github_token, "resolve_github_token", lambda: "fake-token"
    )
    monkeypatch.setattr(
        productivity_route.github_audit, "record_github_use", lambda *a, **k: None
    )
    monkeypatch.setattr(
        productivity_route.github_audit, "record_productivity_request", lambda *a, **k: None
    )
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    _seed_org(conn, org)
    yield {"conn": conn, "org": org}
    conn.execute("DELETE FROM api_keys WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    conn.close()
    productivity_cache.clear()


def test_three_simultaneous_requests_coalesce_to_one_github_fan_out(ctx, monkeypatch):
    """The ticket's acceptance condition, verbatim."""
    conn, org = ctx["conn"], ctx["org"]
    calls = {"n": 0}
    start_gate = threading.Barrier(3)

    def _slow_fetch(*_args, **_kwargs):
        calls["n"] += 1
        # Hold the "in flight" window open long enough that, absent coalescing,
        # the other two threads would race in with their own fan-out too.
        time.sleep(0.2)
        return dict(FAKE_ACTIVITY)

    monkeypatch.setattr(productivity_route.github_commits, "fetch_commit_activity", _slow_fetch)

    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    results: list[dict] = [None, None, None]  # type: ignore[list-item]
    errors: list[BaseException] = []

    def _worker(i: int) -> None:
        try:
            start_gate.wait(timeout=5)
            results[i] = productivity_route.get_series_cached(
                conn, org, "dev-user", "week", now=now
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert calls["n"] == 1, "exactly one GitHub fan-out must be issued for 3 concurrent misses"
    assert all(r is not None for r in results)
    computed_ats = {r["computed_at"] for r in results}
    assert len(computed_ats) == 1, "all three responses must carry the same computed_at"


def test_lock_is_per_cache_key_not_global(ctx):
    """Different keys never contend on the same lock (no unnecessary serialization)."""
    org = ctx["org"]
    lock_a = productivity_cache.lock_for(org, "dev-user", "week")
    lock_b = productivity_cache.lock_for(org, "dev-user", "day")
    lock_a_again = productivity_cache.lock_for(org, "dev-user", "week")
    assert lock_a is lock_a_again
    assert lock_a is not lock_b
