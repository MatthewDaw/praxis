"""Migration 0014: legacy ``finished_at`` rows become the one indexable shape.

Two producers used to write this key; one wrote a bare ``time.time()`` float. Those
epoch rows sort as text outside the ISO bounds ``snapshots_finished_at_idx`` is
range-scanned with, so they silently vanish from every finished-by-date query — a
short answer, not an error. The server is now the sole writer, so no NEW epoch row
can appear; 0014 fixes the ones already stored, and drops stale timestamps from
tickets that are not finished.

Seeds rows at the legacy shapes via raw SQL and runs the migration's statements
against them. Needs a Postgres DSN (same gate as the other snapshot-store tests).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from knowledge import finished_at
from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "0014_normalize_finished_at.sql"
SPACE = SNAPSHOT = "finished-at-migration"
# 2026-07-28T02:29:28.151198+00:00 — the shape one real plan actually carried.
EPOCH = "1785205768.1511981"


@pytest.fixture
def conn(unique_org):
    db.bootstrap()
    c = db.connect()
    org = unique_org
    c.execute("DELETE FROM snapshots WHERE org_id = %s", (org,))
    yield c, org
    c.execute("DELETE FROM snapshots WHERE org_id = %s", (org,))
    c.close()


def _seed(c, org, fid, meta_json):
    c.execute(
        "INSERT INTO snapshots (id, org_id, text, space, snapshot, state, category, meta) "
        "VALUES (%s, %s, %s, %s, %s, 'active', 'requirement', %s::jsonb)",
        (fid, org, "a ticket", SPACE, SNAPSHOT, meta_json),
    )


def _meta(c, org, fid):
    row = c.execute(
        "SELECT meta FROM snapshots WHERE org_id = %s AND id = %s", (org, fid)
    ).fetchone()
    return row[0] if row else None


def _run_migration(c):
    c.execute(MIGRATION.read_text())


def test_epoch_row_is_reformatted_not_re_dated(conn):
    c, org = conn
    _seed(c, org, "legacy-epoch",
          f'{{"build_state": "finished", "finished_at": "{EPOCH}"}}')

    _run_migration(c)

    stamped = _meta(c, org, "legacy-epoch")["finished_at"]
    assert finished_at.is_indexable(stamped), stamped
    # The SAME instant, only reformatted — the migration must not invent a new
    # completion date (that would falsify when the work actually landed).
    expected = datetime.fromtimestamp(float(EPOCH), tz=timezone.utc)
    assert abs((datetime.fromisoformat(stamped) - expected).total_seconds()) < 1


def test_normalized_row_is_found_by_the_indexed_range_query(conn):
    """The whole point: before 0014 this row sorted outside the bounds and returned
    nothing."""
    c, org = conn
    _seed(c, org, "legacy-epoch",
          f'{{"build_state": "finished", "finished_at": "{EPOCH}"}}')
    instant = datetime.fromtimestamp(float(EPOCH), tz=timezone.utc)
    bounds = (
        finished_at.iso_utc(instant.replace(microsecond=0)),
        finished_at.iso_utc(instant.replace(microsecond=999999)),
    )
    ranged = (
        "SELECT id FROM snapshots WHERE org_id = %s "
        "AND meta ->> 'finished_at' BETWEEN %s AND %s"
    )
    assert c.execute(ranged, (org, *bounds)).fetchall() == []

    _run_migration(c)

    assert [r[0] for r in c.execute(ranged, (org, *bounds)).fetchall()] == ["legacy-epoch"]


def test_iso_row_is_left_alone_and_rerun_is_a_noop(conn):
    c, org = conn
    iso = finished_at.now_iso_utc()
    _seed(c, org, "already-iso", f'{{"build_state": "finished", "finished_at": "{iso}"}}')
    _seed(c, org, "legacy-epoch", f'{{"build_state": "finished", "finished_at": "{EPOCH}"}}')

    _run_migration(c)
    after_first = _meta(c, org, "legacy-epoch")["finished_at"]
    assert _meta(c, org, "already-iso")["finished_at"] == iso

    _run_migration(c)
    assert _meta(c, org, "legacy-epoch")["finished_at"] == after_first
    assert _meta(c, org, "already-iso")["finished_at"] == iso


def test_stale_timestamp_is_dropped_from_unfinished_tickets(conn):
    c, org = conn
    _seed(c, org, "regressed",
          f'{{"build_state": "incomplete", "finished_at": "{EPOCH}"}}')
    _seed(c, org, "in-progress",
          f'{{"build_state": "in_progress", "finished_at": "{finished_at.now_iso_utc()}"}}')
    _seed(c, org, "finished",
          f'{{"build_state": "finished", "finished_at": "{finished_at.now_iso_utc()}"}}')

    _run_migration(c)

    assert "finished_at" not in _meta(c, org, "regressed")
    assert "finished_at" not in _meta(c, org, "in-progress")
    assert "finished_at" in _meta(c, org, "finished")   # a real completion survives


def test_finished_ticket_with_no_timestamp_is_backfilled_from_created_at(conn):
    """Every ticket finished before stamping existed. Reports already dated these by
    ``created_at`` (the D33 fallback), so this changes no count — it makes
    ``build_state = 'finished'`` imply a non-null, indexable ``finished_at``, with no
    exceptions for the index to miss."""
    c, org = conn
    _seed(c, org, "old-finish", '{"build_state": "finished"}')
    created = c.execute(
        "SELECT created_at FROM snapshots WHERE org_id = %s AND id = %s",
        (org, "old-finish"),
    ).fetchone()[0]

    _run_migration(c)

    stamped = _meta(c, org, "old-finish")["finished_at"]
    assert finished_at.is_indexable(stamped), stamped
    assert abs(
        (datetime.fromisoformat(stamped) - created.replace(tzinfo=timezone.utc)).total_seconds()
    ) < 1


def test_no_finished_ticket_is_left_without_a_timestamp(conn):
    c, org = conn
    _seed(c, org, "no-stamp", '{"build_state": "finished"}')
    _seed(c, org, "epoch", f'{{"build_state": "finished", "finished_at": "{EPOCH}"}}')
    _seed(c, org, "iso",
          f'{{"build_state": "finished", "finished_at": "{finished_at.now_iso_utc()}"}}')
    _seed(c, org, "unfinished", '{"build_state": "incomplete"}')

    _run_migration(c)

    rows = c.execute(
        "SELECT id FROM snapshots WHERE org_id = %s AND meta ->> 'build_state' = 'finished' "
        "AND NOT (meta ? 'finished_at')",
        (org,),
    ).fetchall()
    assert rows == [], f"finished tickets left with no finished_at: {rows}"
    assert "finished_at" not in _meta(c, org, "unfinished")


def test_unrelated_meta_survives(conn):
    c, org = conn
    _seed(c, org, "legacy-epoch",
          f'{{"build_state": "finished", "finished_at": "{EPOCH}", '
          f'"requirement_id": "R1", "tags": ["auth"]}}')

    _run_migration(c)

    meta = _meta(c, org, "legacy-epoch")
    assert meta["requirement_id"] == "R1"
    assert meta["tags"] == ["auth"]
    assert meta["build_state"] == "finished"
