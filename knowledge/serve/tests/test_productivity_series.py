"""R7 acceptance: the S4 series counts tickets whose meta.finished_at falls
inside each bucket, aggregated across every Praxis space the org can read; a
ticket lacking finished_at contributes to no bucket rather than counting as a
zero-day completion.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from knowledge.serve import db
from knowledge.serve.productivity_series import bucket_counts, s4_series

pytestmark_db = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret); snapshots live in Postgres",
)


# --- pure aggregation (no DB) --------------------------------------------


def test_three_same_day_finishes_bucket_to_three():
    day = datetime(2026, 7, 20, tzinfo=timezone.utc)
    finished_ats = [
        (day + timedelta(hours=1)).isoformat(),
        (day + timedelta(hours=5)).isoformat(),
        (day + timedelta(hours=23)).isoformat(),
    ]
    assert bucket_counts(finished_ats, [day], 86400) == [3]


def test_missing_finished_at_contributes_to_no_bucket():
    day = datetime(2026, 7, 20, tzinfo=timezone.utc)
    finished_ats = [
        (day + timedelta(hours=1)).isoformat(),
        None,
        "",
    ]
    assert bucket_counts(finished_ats, [day], 86400) == [1]


def test_timestamps_sort_into_distinct_buckets():
    day0 = datetime(2026, 7, 20, tzinfo=timezone.utc)
    day1 = day0 + timedelta(days=1)
    finished_ats = [
        (day0 + timedelta(hours=2)).isoformat(),
        (day1 + timedelta(hours=2)).isoformat(),
        (day1 + timedelta(hours=10)).isoformat(),
    ]
    assert bucket_counts(finished_ats, [day0, day1], 86400) == [1, 2]


# --- DB-backed: aggregation spans every space in the org ------------------


@pytestmark_db
def test_s4_series_aggregates_across_every_space_in_org(unique_org):
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    day = datetime(2026, 7, 20, tzinfo=timezone.utc)
    try:
        conn.execute("DELETE FROM snapshots WHERE org_id = %s", (org,))
        rows = [
            # three finished tickets on the same day, spread across two
            # different (space, snapshot) pairs in the same org
            ("t-1", org, "ticket one", "space-a", "prd-space-a", "active",
             "requirement", f'{{"finished_at": "{(day + timedelta(hours=1)).isoformat()}"}}'),
            ("t-2", org, "ticket two", "space-a", "prd-space-a", "active",
             "requirement", f'{{"finished_at": "{(day + timedelta(hours=2)).isoformat()}"}}'),
            ("t-3", org, "ticket three", "space-b", "prd-space-b", "active",
             "requirement", f'{{"finished_at": "{(day + timedelta(hours=3)).isoformat()}"}}'),
            # a ticket that never finished: no finished_at at all
            ("t-4", org, "ticket four", "space-b", "prd-space-b", "active",
             "requirement", "{}"),
        ]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO snapshots "
                "(id, org_id, text, space, snapshot, state, category, meta) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                rows,
            )

        result = s4_series(conn, org, [day], 86400)

        assert result == [3]
    finally:
        conn.execute("DELETE FROM snapshots WHERE org_id = %s", (org,))
        conn.close()
