"""App-layer rate limiting + body-size caps on the public backend.

Mirrors test_server.py's harness: a fresh app per test over a throwaway tenant
(PRAXIS_AUTH_DISABLED=1 -> principal sub="dev-user", so every dev request shares
one bucket — handy for tripping a limit). Each test gets its own app, so the
in-memory limiter starts empty; we also reset it explicitly to be safe.

The 429 test hammers /insights with an EMPTY insight: slowapi's per-route limit
check runs before the endpoint body, so the first N calls 400 (no LLM call) and the
(N+1)th trips the limit -> 429. That keeps the test cheap (no OpenRouter spend).
"""

from __future__ import annotations

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402
from knowledge.serve.rate_limit import LLM_RATE_LIMIT, principal_key  # noqa: E402

# Only a Postgres DSN is needed: the 429/413 paths short-circuit before any LLM
# call, so OPENROUTER_API_KEY is not required here (unlike test_server.py).
pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret) for the org membership check",
)

USER = "dev-user"

# The tight per-route limit ("N/minute") as an int, so the test stays in lockstep
# with the constant in rate_limit.py instead of hard-coding a number.
_LLM_LIMIT = int(LLM_RATE_LIMIT.split("/", 1)[0])


@pytest.fixture
def client(unique_org):
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", USER)
    app = create_app(conn)
    app.state.limiter.reset()  # belt-and-suspenders: never bleed limiter state
    yield TestClient(app, headers={"X-Praxis-Org": org})
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))
    conn.close()


def test_health_is_never_rate_limited(client):
    # /health is the App Runner health check — far past any limit, still 200s.
    for _ in range(80):
        res = client.get("/health")
        assert res.status_code == 200, res.text


def test_insights_trips_429_past_the_limit(client):
    # Empty-insight bodies 400 before any LLM call; once over the tight per-route
    # limit the limiter returns 429 instead.
    statuses = [
        client.post("/insights", json={"insight": ""}).status_code
        for _ in range(_LLM_LIMIT + 3)
    ]
    assert all(s == 400 for s in statuses[:_LLM_LIMIT]), statuses
    assert 429 in statuses[_LLM_LIMIT:], statuses


def test_ingest_oversized_body_is_413(client):
    big = "x" * (128 * 1024 + 1)
    res = client.post("/ingest", json={"documents": [{"text": big}]})
    assert res.status_code == 413, res.text


def test_insights_oversized_body_is_413(client):
    big = "x" * (128 * 1024 + 1)
    res = client.post("/insights", json={"insight": big})
    assert res.status_code == 413, res.text


def test_principal_key_prefers_api_key_then_sub_then_ip():
    # X-Praxis-Key wins (stable per key).
    from starlette.requests import Request

    def _req(headers: dict[str, str]) -> Request:
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return Request(
            {"type": "http", "headers": raw, "client": ("9.9.9.9", 1234)}
        )

    assert principal_key(_req({"X-Praxis-Key": "pxk_abc"})) == "key:pxk_abc"

    # No key: decode the (unverified) JWT and bucket on sub. A fresh-per-request
    # token with the SAME sub must map to the SAME bucket.
    import jwt

    t1 = jwt.encode({"sub": "u-42", "iat": 1}, "k", algorithm="HS256")
    t2 = jwt.encode({"sub": "u-42", "iat": 2}, "k", algorithm="HS256")
    assert principal_key(_req({"Authorization": f"Bearer {t1}"})) == "sub:u-42"
    assert principal_key(_req({"Authorization": f"Bearer {t2}"})) == "sub:u-42"

    # Neither header: fall back to remote address.
    assert principal_key(_req({})) == "ip:9.9.9.9"
