"""Specs for the Productivity page's day-bucketing (D9/D17): buckets are fixed to
``America/Denver`` and stay correct across a daylight-saving transition; the HTTP route never
reads (and therefore never honors) a client-supplied timezone/offset query parameter.

Covers the ticket acceptance condition directly:
  * a range spanning a DST transition buckets each day at ITS OWN local midnight in
    America/Denver (a different UTC offset before vs after the transition), and
  * the response discloses ``"timezone": "America/Denver"``, and
  * an unrecognized/unsupported query parameter on the request (standing in for a
    client-supplied timezone) is silently ignored, never honored, because the route declares
    no such parameter at all.
"""

from __future__ import annotations

from datetime import date

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402
from knowledge.serve.productivity_buckets import BUCKET_TIMEZONE, daily_buckets  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret)",
)

USER = "dev-user"

# 2025-03-09 02:00 America/Denver is the US spring-forward transition (MST -> MDT). Midnight
# on the transition DAY itself (03-09 00:00) is still BEFORE the 2am changeover, so it is the
# LAST midnight in MST; the first midnight in MDT is the following day, 03-10 00:00.
_PRE_TRANSITION = date(2025, 3, 8)
_TRANSITION_DAY = date(2025, 3, 9)
_POST_TRANSITION = date(2025, 3, 10)
_RANGE_END = _POST_TRANSITION


def test_daily_buckets_pure_function_spans_dst_transition():
    result = daily_buckets(_PRE_TRANSITION, _RANGE_END)
    assert result["timezone"] == BUCKET_TIMEZONE == "America/Denver"
    by_date = {b["date"]: b["start_utc"] for b in result["buckets"]}
    assert by_date[_PRE_TRANSITION.isoformat()] == "2025-03-08T07:00:00Z"  # MST: UTC-7
    assert by_date[_TRANSITION_DAY.isoformat()] == "2025-03-09T07:00:00Z"  # still MST (pre-2am changeover)
    assert by_date[_POST_TRANSITION.isoformat()] == "2025-03-10T06:00:00Z"  # first MDT midnight: UTC-6


def test_daily_buckets_rejects_inverted_range():
    with pytest.raises(ValueError):
        daily_buckets(_RANGE_END, _PRE_TRANSITION)


@pytest.fixture
def client(unique_org):
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    for tbl in ("org_members", "orgs"):
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", USER)
    app = create_app(conn)
    yield TestClient(app, headers={"X-Praxis-Org": org})
    for tbl in ("org_members", "orgs"):
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    conn.close()


def test_productivity_buckets_route_spans_dst_and_states_timezone(client):
    resp = client.get(
        "/productivity/buckets",
        params={"start": _PRE_TRANSITION.isoformat(), "end": _RANGE_END.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["timezone"] == "America/Denver"
    by_date = {b["date"]: b["start_utc"] for b in body["buckets"]}
    assert by_date[_PRE_TRANSITION.isoformat()] == "2025-03-08T07:00:00Z"
    assert by_date[_TRANSITION_DAY.isoformat()] == "2025-03-09T07:00:00Z"
    assert by_date[_POST_TRANSITION.isoformat()] == "2025-03-10T06:00:00Z"


def test_productivity_buckets_route_ignores_unrecognized_client_parameter(client):
    """A request supplying an out-of-band parameter that stands in for a client-asserted
    timezone (e.g. a browser's own UTC offset or IANA zone name) must be ignored rather than
    honored: the route declares no such parameter, so FastAPI drops it from the handler
    entirely and the bucketing is byte-identical to the same call without it."""
    baseline = client.get(
        "/productivity/buckets",
        params={"start": _PRE_TRANSITION.isoformat(), "end": _RANGE_END.isoformat()},
    )
    with_extra_param = client.get(
        "/productivity/buckets",
        params={
            "start": _PRE_TRANSITION.isoformat(),
            "end": _RANGE_END.isoformat(),
            "timezone": "UTC",
            "client_utc_offset_minutes": "-120",
        },
    )
    assert with_extra_param.status_code == 200, with_extra_param.text
    assert with_extra_param.json() == baseline.json()
    assert with_extra_param.json()["timezone"] == "America/Denver"
