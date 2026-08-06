"""``meta.finished_at`` — the ticket completion timestamp. The server owns it.

A completion timestamp is a fact about a write the SERVER makes. A client that
stamps one is guessing at a clock it does not own: it can be skewed, it can be
stale, and it can be forged. So no client — no build worker, no frontend — ever
supplies this value or has to think about it. A caller sets ``build_state``; the
server dates it. Every server-side path that can move a ticket's ``build_state``
routes through here:

  * ``PostgresVectorGraph.release_requirement``  — the lease-release path
    (``POST /requirements/{cid}/release``), stamped/cleared in SQL from the DB clock.
  * ``FactsCandidates.update``                   — the meta-merge path
    (``PATCH /candidates/{cid}``, i.e. ``_praxis.patch_meta``), stamped here.
  * ``PostgresVectorGraph.regress_requirements`` — the regress path, which clears it.

ONE RULE, applied wherever ``build_state`` is written (:func:`resolve`): setting it
to ``finished`` stamps the server clock; setting it to anything else drops the key;
a write that does not touch ``build_state`` leaves it alone. So a ticket that
yielded, regressed, or is merely claimed can never read back as done work it did
not complete, and a re-finish keeps the LATEST completion. An inbound
``finished_at`` from a caller is discarded before the merge, so it can never win.

THE SHAPE is a fixed-width, zero-padded UTC ISO-8601 string
(``2026-07-25T03:50:06.740712+00:00``) — the exact shape migration 0013's
``snapshots_finished_at_idx`` indexes. That index is a TEXT expression index (a
``timestamptz`` cast is STABLE, not IMMUTABLE, so Postgres refuses it), and this
format's lexicographic order matches its chronological order, so a text
``BETWEEN`` is a correct range scan. Any OTHER shape — notably the bare
``time.time()`` float a client used to write — sorts as text somewhere else
entirely and silently falls out of every range query that uses the index. A short
answer, not an error, which is why it survived so long.

:func:`parse` stays deliberately tolerant of that legacy epoch shape. Migration
0014 normalizes the rows that exist today, but a snapshot restored from an older
dump (or loaded from an external org source) can still carry one, and dropping
real completed work from a report is worse than parsing a shape we no longer
write. Tolerance on READ, one producer on WRITE.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

FINISHED_AT = "finished_at"
BUILD_STATE = "build_state"
FINISHED = "finished"

# The SQL that mints the value on the Postgres paths. Byte-identical in shape to
# :func:`now_iso_utc`; keep the two in lockstep.
SQL_NOW_ISO_UTC = (
    "to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS.US') || '+00:00'"
)

_ISO_FMT = "%Y-%m-%dT%H:%M:%S.%f"

# The one shape ``snapshots_finished_at_idx`` sorts correctly: zero-padded, UTC,
# microsecond precision, explicit ``+00:00`` offset.
INDEXABLE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")


def iso_utc(dt: datetime) -> str:
    """Render ``dt`` in the indexable shape (naive input is read as UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(_ISO_FMT) + "+00:00"


def now_iso_utc() -> str:
    """The server clock, in the indexable shape."""
    return iso_utc(datetime.now(timezone.utc))


def is_indexable(value: object) -> bool:
    """True iff ``value`` sorts correctly under ``snapshots_finished_at_idx``."""
    return isinstance(value, str) and bool(INDEXABLE.match(value))


def parse(raw: object) -> datetime | None:
    """Parse a stored ``finished_at`` in either shape — the indexable ISO-8601
    string, or the legacy bare epoch-seconds float rendered as text
    (``"1785205768.1511981"``). ``None`` for anything that matches neither, so one
    malformed value never fails a whole report. See the module docstring on why
    the legacy shape is still read but never written."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        raw = repr(float(raw))
    if not isinstance(raw, str) or not raw.strip():
        return None
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


def resolve(incoming: dict, merged: dict) -> dict:
    """Apply the one rule to ``merged`` in place, and return it.

    ``incoming`` is the caller's patch (what this write actually SETS); ``merged``
    is the fact's resulting meta. A write that does not set ``build_state`` leaves
    any existing ``finished_at`` untouched — only a state transition dates a
    ticket, so an unrelated bookkeeping patch can never drag a completion forward.
    """
    if BUILD_STATE not in incoming:
        return merged
    if incoming[BUILD_STATE] == FINISHED:
        merged[FINISHED_AT] = now_iso_utc()
    else:
        merged.pop(FINISHED_AT, None)
    return merged


def drop_client_value(meta: dict | None) -> None:
    """Discard any caller-supplied ``finished_at`` from an inbound patch, in place.
    Callers merge the REMAINDER and then call :func:`resolve`, so a caller's value
    can never win — see the module docstring."""
    if isinstance(meta, dict):
        meta.pop(FINISHED_AT, None)
