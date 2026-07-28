"""S4: the ticket-completion series (R7).

Counts Praxis tickets (``category="requirement"`` snapshot facts) whose
``meta.build_state == "finished"`` — the SAME predicate ``agent_factory``'s own
``_ticket_state`` uses to decide a ticket is done (``M_BUILD_STATE``,
``unfinished_ids``) — falls inside each fixed time bucket. Aggregated across
every space in the caller's org: spaces are org-shared (any org member can
read every space — see ``spaces_store.py``), so "every Praxis space the
authenticated user can read" is exactly every space row for that ``org_id``,
with no per-space membership filter needed. The same reasoning extends across
orgs: the fetches below take a LIST of org ids so S4 can span EVERY org the
requesting user belongs to, not just whichever one ``X-Praxis-Org`` selected —
"tickets I completed" is a property of the person, and scoping it to the active
org made real completions in the user's other orgs read as a flat zero.

Every finished ticket is dated by ``meta.finished_at`` when it was stamped, or
by its own ``created_at`` column otherwise (D33): ``finished_at`` is a fairly
recent addition (stamped by TWO independent writers that disagree on shape —
the backend's own lease-release path, ``postgres_vector_graph.
release_requirement``/migration 0013, writes a fixed-format UTC ISO-8601
string; ``agent_factory``'s ticket-loop, ``_ticket_state.release``, writes a
raw ``time.time()`` epoch-seconds float; :func:`_parse_finished_at` accepts
both), so most tickets finished before it existed carry no ``finished_at`` at
all. Rather than drop that real, completed work from the count entirely, a
finished ticket with no ``finished_at`` is dated to its own creation date —
every ticket marked complete counts toward some bucket.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

import psycopg


def _org_id_list(org_ids: str | Sequence[str]) -> list[str]:
    """Normalize the org selector every fetch below accepts.

    A bare ``str`` (the historical single-org call shape, still used by callers that
    genuinely only care about one org) and a sequence of org ids are both accepted;
    the result is always a de-duplicated list, order-preserving, so ``= ANY(%s)``
    sees exactly one parameter shape.
    """
    if isinstance(org_ids, str):
        return [org_ids]
    seen: dict[str, None] = {}
    for org_id in org_ids:
        seen.setdefault(org_id, None)
    return list(seen)


# The one predicate that decides a snapshot row is a completed ticket, shared verbatim
# by every fetch below so the aggregate, the per-org breakdown and the instrumentation
# date can never drift apart. ``build_state = 'finished'`` is agent_factory's own
# done-predicate; the COALESCE dates a finished ticket by ``meta.finished_at`` when it
# was stamped and by its own ``created_at`` column otherwise (D33).
_FINISHED_AT_EXPR = (
    "COALESCE(meta ->> 'finished_at', "
    "  to_char(created_at AT TIME ZONE 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS.US') || '+00:00')"
)
_FINISHED_TICKET_WHERE = (
    "WHERE org_id = ANY(%s) AND category = 'requirement' "
    "AND meta ->> 'build_state' = 'finished'"
)


def _parse_finished_at(raw: str) -> datetime | None:
    """Parse a ``finished_at`` value in either shape it's written in (see module
    docstring): a UTC ISO-8601 string, or a bare epoch-seconds float rendered as
    text (e.g. ``"1785205768.1511981"``). Returns ``None`` for anything that
    matches neither, so one malformed value never fails the whole series."""
    try:
        finished = datetime.fromisoformat(raw)
    except ValueError:
        try:
            finished = datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    return finished


def bucket_counts(
    finished_ats: list[str | None],
    bucket_starts: list[datetime],
    bucket_seconds: float,
) -> list[int]:
    """Count how many ``finished_ats`` values land in each bucket.

    Buckets are half-open ``[start, start + bucket_seconds)``, checked in the
    given order (the first bucket a timestamp falls in wins). A missing/empty/
    unparseable value is skipped — it lands in no bucket.
    """
    counts = [0] * len(bucket_starts)
    for raw in finished_ats:
        if not raw:
            continue
        finished = _parse_finished_at(raw)
        if finished is None:
            continue
        for i, start in enumerate(bucket_starts):
            if start <= finished < start + timedelta(seconds=bucket_seconds):
                counts[i] += 1
                break
    return counts


def fetch_ticket_finished_ats(
    conn: psycopg.Connection, org_ids: str | Sequence[str]
) -> list[str | None]:
    """Every ``build_state = 'finished'`` ticket's completion timestamp across the spaces of
    ``org_ids`` (a single org id, or every org the requesting user belongs to): ``meta.finished_at``
    when it was stamped, else the ticket's own ``created_at`` (a real, always-present column) as
    the completion date. Every finished ticket contributes a bucket — by design, per D33: a ticket
    finished before ``finished_at`` stamping existed still counts, dated to when it was created,
    rather than being dropped from the count entirely."""
    rows = conn.execute(
        f"SELECT {_FINISHED_AT_EXPR} FROM snapshots {_FINISHED_TICKET_WHERE}",
        (_org_id_list(org_ids),),
    ).fetchall()
    return [r[0] for r in rows]


def fetch_ticket_finished_ats_by_org(
    conn: psycopg.Connection, org_ids: str | Sequence[str]
) -> dict[str, list[str | None]]:
    """:func:`fetch_ticket_finished_ats`, grouped by owning org id.

    Same rows, same predicate, same date expression — only the grouping differs, so the
    per-org breakdown can never disagree with the aggregate. Every org in ``org_ids`` is
    present as a key, including orgs with no finished tickets at all (an empty list),
    so a caller can render a confirmed zero rather than an absent series.
    """
    ids = _org_id_list(org_ids)
    by_org: dict[str, list[str | None]] = {org_id: [] for org_id in ids}
    rows = conn.execute(
        f"SELECT org_id, {_FINISHED_AT_EXPR} FROM snapshots {_FINISHED_TICKET_WHERE}",
        (ids,),
    ).fetchall()
    for org_id, finished_at in rows:
        by_org.setdefault(org_id, []).append(finished_at)
    return by_org


def s4_series(
    conn: psycopg.Connection,
    org_ids: str | Sequence[str],
    bucket_starts: list[datetime],
    bucket_seconds: float,
) -> list[int]:
    """The S4 value for each bucket: finished-ticket counts summed across ``org_ids``.

    ``org_ids`` is every org the requesting user belongs to (a bare org id is also
    accepted): a ticket completed in ANY of the user's orgs counts, because "tickets
    I completed" is a property of the person, not of whichever org the UI happens to
    have selected.
    """
    return bucket_counts(
        fetch_ticket_finished_ats(conn, org_ids), bucket_starts, bucket_seconds
    )


def s4_series_by_org(
    conn: psycopg.Connection,
    org_ids: str | Sequence[str],
    bucket_starts: list[datetime],
    bucket_seconds: float,
) -> dict[str, list[int]]:
    """The S4 per-bucket counts broken down per org: ``{org_id: [count, ...]}``.

    Summing the lists position-wise reproduces :func:`s4_series` exactly. Every org in
    ``org_ids`` is a key even when all its counts are zero — filtering empty orgs out is
    a presentation decision for the caller, not this layer's.
    """
    return {
        org_id: bucket_counts(finished_ats, bucket_starts, bucket_seconds)
        for org_id, finished_ats in fetch_ticket_finished_ats_by_org(conn, org_ids).items()
    }


def instrumentation_date(finished_ats: list[str | None]) -> str | None:
    """The earliest ``finished_at`` in ``finished_ats``, or ``None`` if none exist.

    This IS the S4 instrumentation start date (D1/D27): finish-timestamp stamping
    only began recording once the first ticket ever transitioned to ``finished``,
    so the earliest recorded value is exactly the point before which "no data" and
    "zero tickets finished" are indistinguishable — the boundary a chart must grey
    and annotate ("ticket history starts <date>") rather than render as a truthful
    zero.
    """
    parsed = [_parse_finished_at(raw) for raw in finished_ats if raw]
    parsed = [dt for dt in parsed if dt is not None]
    if not parsed:
        return None
    return min(parsed).isoformat()


def s4_instrumentation_date(
    conn: psycopg.Connection, org_ids: str | Sequence[str]
) -> str | None:
    """The S4 instrumentation-start date: the EARLIEST ``finished_at`` ever recorded across
    every space of every org in ``org_ids`` (see :func:`instrumentation_date`), or ``None``
    when no ticket in any of them ever finished.

    It must span the same org set the aggregate series does: an instrumentation date taken
    from only one of the user's orgs would grey out buckets that another org has real data
    for."""
    return instrumentation_date(fetch_ticket_finished_ats(conn, org_ids))
