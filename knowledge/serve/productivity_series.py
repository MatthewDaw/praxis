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
