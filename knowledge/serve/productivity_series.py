"""S4: the ticket-completion series (R7).

Counts Praxis tickets (``category="requirement"`` snapshot facts) whose
``meta.finished_at`` (stamped only by the ``release(state="finished")`` path,
see migration 0013 / R6) falls inside each fixed time bucket. Aggregated
across every space in the caller's org: spaces are org-shared (any org member
can read every space — see ``spaces_store.py``), so "every Praxis space the
authenticated user can read" is exactly every space row for that ``org_id``,
with no per-space membership filter needed.

A ticket that never transitioned to ``finished`` carries no ``finished_at``
and is skipped entirely — it contributes to no bucket, never a zero-day
completion.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg


def bucket_counts(
    finished_ats: list[str | None],
    bucket_starts: list[datetime],
    bucket_seconds: float,
) -> list[int]:
    """Count how many ``finished_ats`` values land in each bucket.

    Buckets are half-open ``[start, start + bucket_seconds)``, checked in the
    given order (the first bucket a timestamp falls in wins). A missing/empty
    value is skipped — it lands in no bucket.
    """
    counts = [0] * len(bucket_starts)
    for raw in finished_ats:
        if not raw:
            continue
        finished = datetime.fromisoformat(raw)
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        for i, start in enumerate(bucket_starts):
            if start <= finished < start + timedelta(seconds=bucket_seconds):
                counts[i] += 1
                break
    return counts


def fetch_ticket_finished_ats(conn: psycopg.Connection, org_id: str) -> list[str | None]:
    """Every ticket's ``meta.finished_at`` (or ``None``) across ``org_id``'s spaces."""
    rows = conn.execute(
        "SELECT meta ->> 'finished_at' FROM snapshots "
        "WHERE org_id = %s AND category = 'requirement'",
        (org_id,),
    ).fetchall()
    return [r[0] for r in rows]


def s4_series(
    conn: psycopg.Connection,
    org_id: str,
    bucket_starts: list[datetime],
    bucket_seconds: float,
) -> list[int]:
    """The S4 value for each bucket: finished-ticket counts, org-wide."""
    return bucket_counts(
        fetch_ticket_finished_ats(conn, org_id), bucket_starts, bucket_seconds
    )


def instrumentation_date(finished_ats: list[str | None]) -> str | None:
    """The earliest ``finished_at`` in ``finished_ats``, or ``None`` if none exist.

    This IS the S4 instrumentation start date (D1/D27): finish-timestamp stamping
    only began recording once the first ticket ever transitioned to ``finished``,
    so the earliest recorded value is exactly the point before which "no data" and
    "zero tickets finished" are indistinguishable — the boundary a chart must grey
    and annotate ("ticket history starts <date>") rather than render as a truthful
    zero.
    """
    values = [raw for raw in finished_ats if raw]
    if not values:
        return None
    return min(values)


def s4_instrumentation_date(conn: psycopg.Connection, org_id: str) -> str | None:
    """The org's S4 instrumentation-start date: the earliest ``finished_at`` ever
    recorded across every space in ``org_id`` (see :func:`instrumentation_date`)."""
    return instrumentation_date(fetch_ticket_finished_ats(conn, org_id))
