"""The S4 (ticket-completion-series) query filters `snapshots` by the ticket finish
timestamp (`meta->>'finished_at'`). `snapshots_meta_gin` (a GIN index over the whole
`meta` jsonb column) serves containment/key-existence lookups, not a range predicate
on an extracted key, so a `BETWEEN` scan over `finished_at` fell back to a full
Seq Scan of every snapshot row. Migration 0013 adds a btree expression index on the
extracted `finished_at` text; this test proves a representative range query actually
uses it (R36 acceptance condition).

Needs a Postgres DSN (same gate as the other snapshot-store tests). Seeds enough
rows for the planner to prefer the index over a sequential scan, then runs `EXPLAIN`
(no `ANALYZE` — this reads the plan only, no query execution) on a narrow
`finished_at` range and asserts an Index Scan on `snapshots_finished_at_idx` and no
Seq Scan of `snapshots` appear in the plan.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret); the index lives in Postgres",
)

SPACE = "finished-at-idx-eval"
SNAPSHOT = "finished-at-idx-eval"
ROW_COUNT = 5000


@pytest.fixture
def conn(unique_org):
    db.bootstrap()
    c = db.connect()
    c.execute("DELETE FROM snapshots WHERE org_id = %s", (unique_org,))
    yield c, unique_org
    c.execute("DELETE FROM snapshots WHERE org_id = %s", (unique_org,))
    c.close()


def _seed(c, org: str, base: datetime) -> None:
    """Insert ROW_COUNT rows with `finished_at` spread one minute apart from ``base``,
    so a narrow window is a small, selective slice of a large table (the planner
    needs enough rows to prefer the index over a Seq Scan on tiny test data)."""
    rows = [
        (
            f"ft-{i}",
            org,
            "a finished ticket",
            SPACE,
            SNAPSHOT,
            "active",
            f'{{"finished_at": "{(base + timedelta(minutes=i)).isoformat()}"}}',
        )
        for i in range(ROW_COUNT)
    ]
    with c.cursor() as cur:
        cur.executemany(
            "INSERT INTO snapshots (id, org_id, text, space, snapshot, state, meta) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
            rows,
        )
    c.execute("ANALYZE snapshots")


def _explain_range_query(c, org: str, start: datetime, end: datetime) -> str:
    # Compared as text against two zero-padded UTC ISO-8601 bounds (see migration
    # 0013): lexicographic order matches chronological order for this fixed format.
    rows = c.execute(
        "EXPLAIN SELECT id FROM snapshots "
        "WHERE org_id = %s AND meta ->> 'finished_at' BETWEEN %s AND %s",
        (org, start.isoformat(), end.isoformat()),
    ).fetchall()
    return "\n".join(r[0] for r in rows)


def test_finished_at_range_query_uses_index_not_seq_scan(conn):
    c, org = conn
    base = datetime.now(timezone.utc) - timedelta(days=1)
    _seed(c, org, base)
    start = base + timedelta(minutes=1000)
    end = base + timedelta(minutes=1010)

    plan = _explain_range_query(c, org, start, end)

    assert "Index Scan" in plan, plan
    assert "snapshots_finished_at_idx" in plan, plan
    assert "Seq Scan" not in plan, plan
