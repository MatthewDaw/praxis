"""R7 acceptance: the S4 series counts tickets whose meta.finished_at falls
inside each bucket, aggregated across every Praxis space the org can read; a
ticket lacking finished_at contributes to no bucket rather than counting as a
zero-day completion.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from knowledge.serve import db
from knowledge.serve.productivity_series import (
    bucket_counts,
    instrumentation_date,
    s4_instrumentation_date,
    s4_series,
    s4_series_by_org,
)

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


def test_epoch_seconds_finished_at_is_counted():
    """``agent_factory``'s ticket-loop (``_ticket_state.release``) stamps
    ``finished_at`` as a raw ``time.time()`` epoch-seconds float, not the ISO-8601
    string the backend's own release path writes -- both shapes must count."""
    day = datetime(2026, 7, 20, tzinfo=timezone.utc)
    epoch_str = str((day + timedelta(hours=5)).timestamp())
    assert bucket_counts([epoch_str], [day], 86400) == [1]


def test_mixed_iso_and_epoch_finished_at_both_count():
    day = datetime(2026, 7, 20, tzinfo=timezone.utc)
    finished_ats = [
        (day + timedelta(hours=1)).isoformat(),
        str((day + timedelta(hours=2)).timestamp()),
    ]
    assert bucket_counts(finished_ats, [day], 86400) == [2]


def test_unparseable_finished_at_contributes_to_no_bucket():
    day = datetime(2026, 7, 20, tzinfo=timezone.utc)
    finished_ats = [(day + timedelta(hours=1)).isoformat(), "not-a-timestamp"]
    assert bucket_counts(finished_ats, [day], 86400) == [1]


# --- R27: S4 instrumentation-start date (the earliest finished_at ever) --


def test_instrumentation_date_is_the_earliest_finished_at():
    assert instrumentation_date(
        [
            "2026-07-25T09:00:00+00:00",
            "2026-07-25T07:17:15+00:00",
            "2026-07-25T10:30:00+00:00",
        ]
    ) == "2026-07-25T07:17:15+00:00"


def test_instrumentation_date_skips_missing_values():
    assert instrumentation_date([None, "", "2026-07-25T07:17:15+00:00"]) == (
        "2026-07-25T07:17:15+00:00"
    )


def test_instrumentation_date_is_none_when_nothing_ever_finished():
    assert instrumentation_date([None, "", None]) is None


def test_instrumentation_date_compares_mixed_iso_and_epoch_chronologically():
    """A lexicographic min on raw strings would wrongly pick the epoch value here
    (it starts with '1', which sorts before '2026...') even though the ISO value
    is chronologically earlier -- the comparison must be by parsed instant."""
    earlier_iso = "2026-07-20T00:00:00+00:00"
    later_epoch = str(datetime(2026, 7, 25, tzinfo=timezone.utc).timestamp())
    assert instrumentation_date([later_epoch, earlier_iso]) == earlier_iso


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
             "requirement",
             f'{{"build_state": "finished", "finished_at": "{(day + timedelta(hours=1)).isoformat()}"}}'),
            ("t-2", org, "ticket two", "space-a", "prd-space-a", "active",
             "requirement",
             f'{{"build_state": "finished", "finished_at": "{(day + timedelta(hours=2)).isoformat()}"}}'),
            ("t-3", org, "ticket three", "space-b", "prd-space-b", "active",
             "requirement",
             f'{{"build_state": "finished", "finished_at": "{(day + timedelta(hours=3)).isoformat()}"}}'),
            # a ticket that never finished: build_state is not "finished" at all
            ("t-4", org, "ticket four", "space-b", "prd-space-b", "active",
             "requirement", '{"build_state": "incomplete"}'),
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


@pytestmark_db
def test_s4_instrumentation_date_is_the_earliest_finish_across_the_org(unique_org):
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    day = datetime(2026, 7, 20, tzinfo=timezone.utc)
    try:
        conn.execute("DELETE FROM snapshots WHERE org_id = %s", (org,))
        rows = [
            ("t-1", org, "ticket one", "space-a", "prd-space-a", "active",
             "requirement",
             f'{{"build_state": "finished", "finished_at": "{(day + timedelta(hours=3)).isoformat()}"}}'),
            # the earliest finish, in a DIFFERENT space -- must still win.
            ("t-2", org, "ticket two", "space-b", "prd-space-b", "active",
             "requirement",
             f'{{"build_state": "finished", "finished_at": "{(day + timedelta(hours=1)).isoformat()}"}}'),
            ("t-3", org, "ticket three", "space-a", "prd-space-a", "active",
             "requirement", '{"build_state": "in_progress"}'),
        ]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO snapshots "
                "(id, org_id, text, space, snapshot, state, category, meta) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                rows,
            )

        result = s4_instrumentation_date(conn, org)

        assert result == (day + timedelta(hours=1)).isoformat()
    finally:
        conn.execute("DELETE FROM snapshots WHERE org_id = %s", (org,))
        conn.close()


@pytestmark_db
def test_s4_instrumentation_date_is_none_when_no_ticket_ever_finished(unique_org):
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    try:
        conn.execute("DELETE FROM snapshots WHERE org_id = %s", (org,))
        conn.execute(
            "INSERT INTO snapshots (id, org_id, text, space, snapshot, state, category, meta) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            ("t-1", org, "ticket one", "space-a", "prd-space-a", "active", "requirement", "{}"),
        )

        assert s4_instrumentation_date(conn, org) is None
    finally:
        conn.execute("DELETE FROM snapshots WHERE org_id = %s", (org,))
        conn.close()


@pytestmark_db
def test_finished_ticket_with_no_finished_at_counts_on_its_created_at(unique_org):
    """D33: a ticket finished before ``finished_at`` stamping existed (most historical
    completions) still counts -- dated to its own ``created_at`` -- rather than being
    dropped from the series entirely."""
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    day = datetime(2026, 7, 20, tzinfo=timezone.utc)
    try:
        conn.execute("DELETE FROM snapshots WHERE org_id = %s", (org,))
        conn.execute(
            "INSERT INTO snapshots "
            "(id, org_id, text, space, snapshot, state, category, meta, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
            ("t-1", org, "ticket one", "space-a", "prd-space-a", "active",
             "requirement", '{"build_state": "finished"}', day + timedelta(hours=5)),
        )

        result = s4_series(conn, org, [day], 86400)

        assert result == [1]
    finally:
        conn.execute("DELETE FROM snapshots WHERE org_id = %s", (org,))
        conn.close()


# --- DB-backed: aggregation spans every ORG the user belongs to -----------


def _finished_ticket_row(ticket_id, org, space, finished_at):
    return (
        ticket_id, org, f"ticket {ticket_id}", space, f"prd-{space}", "active", "requirement",
        f'{{"build_state": "finished", "finished_at": "{finished_at.isoformat()}"}}',
    )


def _insert_snapshots(conn, rows):
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO snapshots "
            "(id, org_id, text, space, snapshot, state, category, meta) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            rows,
        )


@pytestmark_db
def test_s4_series_sums_across_every_org_in_the_list(unique_org):
    """The reported bug at the series layer: a list of org ids counts tickets from ALL of
    them, so completions in an org other than the active one are never dropped."""
    db.bootstrap()
    conn = db.connect()
    org_a, org_b = unique_org + "_a", unique_org + "_b"
    day = datetime(2026, 7, 20, tzinfo=timezone.utc)
    try:
        conn.execute("DELETE FROM snapshots WHERE org_id = ANY(%s)", ([org_a, org_b],))
        _insert_snapshots(conn, [
            _finished_ticket_row("t-1", org_a, "space-a", day + timedelta(hours=1)),
            _finished_ticket_row("t-2", org_b, "space-b", day + timedelta(hours=2)),
            _finished_ticket_row("t-3", org_b, "space-c", day + timedelta(hours=3)),
        ])

        assert s4_series(conn, [org_a, org_b], [day], 86400) == [3]
        # ...and the single-org call shape still means exactly one org.
        assert s4_series(conn, org_a, [day], 86400) == [1]
    finally:
        conn.execute("DELETE FROM snapshots WHERE org_id = ANY(%s)", ([org_a, org_b],))
        conn.close()


@pytestmark_db
def test_s4_series_by_org_keys_every_requested_org_including_empty_ones(unique_org):
    db.bootstrap()
    conn = db.connect()
    org_a, org_b, org_c = unique_org + "_a", unique_org + "_b", unique_org + "_c"
    orgs = [org_a, org_b, org_c]
    day0 = datetime(2026, 7, 20, tzinfo=timezone.utc)
    day1 = day0 + timedelta(days=1)
    try:
        conn.execute("DELETE FROM snapshots WHERE org_id = ANY(%s)", (orgs,))
        _insert_snapshots(conn, [
            _finished_ticket_row("t-1", org_a, "space-a", day0 + timedelta(hours=1)),
            _finished_ticket_row("t-2", org_b, "space-b", day1 + timedelta(hours=1)),
            _finished_ticket_row("t-3", org_b, "space-b", day1 + timedelta(hours=2)),
        ])

        by_org = s4_series_by_org(conn, orgs, [day0, day1], 86400)

        assert by_org == {org_a: [1, 0], org_b: [0, 2], org_c: [0, 0]}
        # The breakdown sums position-wise to the aggregate.
        aggregate = s4_series(conn, orgs, [day0, day1], 86400)
        assert [sum(v) for v in zip(*by_org.values())] == aggregate == [1, 2]
    finally:
        conn.execute("DELETE FROM snapshots WHERE org_id = ANY(%s)", (orgs,))
        conn.close()


@pytestmark_db
def test_s4_instrumentation_date_is_the_earliest_finish_across_every_org(unique_org):
    db.bootstrap()
    conn = db.connect()
    org_a, org_b = unique_org + "_a", unique_org + "_b"
    day = datetime(2026, 7, 20, tzinfo=timezone.utc)
    try:
        conn.execute("DELETE FROM snapshots WHERE org_id = ANY(%s)", ([org_a, org_b],))
        _insert_snapshots(conn, [
            _finished_ticket_row("t-1", org_a, "space-a", day + timedelta(hours=5)),
            # the earliest finish lives in the OTHER org -- it must still win.
            _finished_ticket_row("t-2", org_b, "space-b", day + timedelta(hours=1)),
        ])

        assert s4_instrumentation_date(conn, [org_a, org_b]) == (
            (day + timedelta(hours=1)).isoformat()
        )
    finally:
        conn.execute("DELETE FROM snapshots WHERE org_id = ANY(%s)", ([org_a, org_b],))
        conn.close()
