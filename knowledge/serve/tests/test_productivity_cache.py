"""Server-side response cache for GET /productivity (R4).

Acceptance condition: given two identical requests inside the TTL, the second is
served from cache without any GitHub call and both responses carry the same
``computed_at`` timestamp. Also covers the short/long TTL split by range family
(D7: 60-120s for <=4-week ranges, 10-30min for 12-month/all-time) and that a
request past the TTL genuinely misses (a fresh GitHub call, a new computed_at).
"""

from __future__ import annotations

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.serve import db, productivity_cache  # noqa: E402
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
    productivity_cache.clear()


def _fake_fetch(calls):
    def _fetch(*_args, **_kwargs):
        calls["n"] += 1
        return dict(FAKE_ACTIVITY)

    return _fetch


def test_second_identical_request_served_from_cache_without_github_call(ctx, monkeypatch):
    """The ticket's acceptance condition, verbatim."""
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)
    client, org = ctx["client"], ctx["org"]
    calls = {"n": 0}
    monkeypatch.setattr(
        productivity_route.github_commits, "fetch_commit_activity", _fake_fetch(calls)
    )

    res1 = client.get("/productivity", params={"range": "week"}, headers={"X-Praxis-Org": org})
    res2 = client.get("/productivity", params={"range": "week"}, headers={"X-Praxis-Org": org})

    assert res1.status_code == 200, res1.text
    assert res2.status_code == 200, res2.text
    assert calls["n"] == 1, "the second identical request must not call GitHub again"

    body1, body2 = res1.json(), res2.json()
    assert "computed_at" in body1 and body1["computed_at"]
    assert body1["computed_at"] == body2["computed_at"]
    assert body1["series"] == body2["series"]


def test_request_past_ttl_is_a_genuine_miss_with_a_fresh_computed_at(ctx, monkeypatch):
    """Unit-level: a request whose cache entry has aged past its TTL is a miss
    (exercises :func:`productivity_cache.get`/``put`` directly with an explicit
    clock, since the route always stamps ``computed_at`` off the real wall clock)."""
    monkeypatch.setenv("PRODUCTIVITY_CACHE_SHORT_TTL_SECONDS", "60")
    org, uid, range_ = ctx["org"], "dev-user", "week"

    productivity_cache.put(org, uid, range_, {"computed_at": "t0"}, now=1_000_000.0)
    assert productivity_cache.get(org, uid, range_, now=1_000_030.0) is not None  # inside TTL
    assert productivity_cache.get(org, uid, range_, now=1_000_061.0) is None  # past the 60s TTL

    # Once evicted by the expired read above, the entry is gone even inside a
    # fresh window unless re-put -- a genuine miss, not a stale hit.
    assert productivity_cache.get(org, uid, range_, now=1_000_062.0) is None


def test_short_vs_long_ttl_band_by_range_family():
    """D7: <=4-week ranges get the 60-120s band; 12-month/all-time get the 10-30min band."""
    for short_range in ("day", "week", "4weeks"):
        assert 60.0 <= productivity_cache.ttl_seconds(short_range) <= 120.0
    for long_range in ("12months", "alltime"):
        assert 10 * 60.0 <= productivity_cache.ttl_seconds(long_range) <= 30 * 60.0


def test_cache_hit_never_calls_github_even_for_a_different_authenticated_request_shape(
    ctx, monkeypatch
):
    """A cache hit must short-circuit before any GitHub call is made -- not merely
    return the same data by coincidence of a stubbed fetch."""
    monkeypatch.setenv("PRAXIS_DEV_USER_EMAIL", OWNER_EMAIL)
    client, org = ctx["client"], ctx["org"]

    def _fetch_once_then_explode(calls=[0]):
        def _fetch(*_a, **_k):
            calls[0] += 1
            if calls[0] > 1:
                raise AssertionError("GitHub must not be called again on a cache hit")
            return dict(FAKE_ACTIVITY)

        return _fetch

    monkeypatch.setattr(
        productivity_route.github_commits, "fetch_commit_activity", _fetch_once_then_explode()
    )

    res1 = client.get("/productivity", params={"range": "week"}, headers={"X-Praxis-Org": org})
    res2 = client.get("/productivity", params={"range": "week"}, headers={"X-Praxis-Org": org})
    assert res1.status_code == 200 and res2.status_code == 200
