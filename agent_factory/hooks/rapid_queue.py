#!/usr/bin/env python3
"""rapid_queue.py — the capture spool behind the ``af-rapid-queue`` skill.

THE PROBLEM. While debugging a running app the owner spots a fix every few minutes. Each one is a
real request, but acting on it *now* derails whatever the session is mid-way through, and holding it
"in the conversation" loses it the moment the context is summarized or the session dies. The owner
wants to fire the request and walk away: never derailed, never lost, eventually finished.

THE DIVISION OF LABOUR. Praxis is still the SINGLE SOURCE OF DYNAMIC TRUTH for the queue itself: a
captured request becomes an ordinary ``build_state="incomplete"`` requirement ticket in the project's
``prd-<project>`` snapshot, which means ``incomplete_requirements`` finds it, af-build drains it, and
``build_completeness_gate`` refuses to let a run stop while it is outstanding. **No new queue, no new
drain loop, and no new completion semantics are invented here** — a rapid-queue ticket is just a
ticket.

WHAT THIS FILE IS, THEN. Only the write-ahead log in front of that Praxis write. The one thing the
skill must never do is drop a request on the floor between "the owner typed it" and "Praxis
acknowledged the ticket" — and that window contains an MCP round-trip that can fail (Praxis down,
auth expired, the session killed mid-turn). So capture is a local append that cannot realistically
fail, and the Praxis write is a *promotion* of that record. This does NOT weaken the constitution's
fail-closed rule: the spool holds **no build state and no validation state** — only raw un-filed
request text — and an entry is dropped from the pending set the instant its ticket id exists.

CONTRAST WITH THE JOB MAILBOX (``knowledge/serve/box_service_mailbox.py``). Same discipline,
opposite direction. The mailbox carries an operator message INTO a running remote job and is drained
destructively at a ticket boundary, because a message is *informational* — showing it once is
delivery. A rapid-queue entry is *work*, so:

  * it is never drained by being read — only ``mark_filed`` (which requires a real ticket id) can
    retire one, so a surfaced-but-unfiled request comes back at the next boundary rather than
    evaporating; and
  * it is stored per PROJECT under ``~/.praxis/rapid-queue/`` rather than inside a worktree, so it
    survives a branch switch, a worktree removal, or an af-build worker cleaning up after itself,
    and every parallel worker in the same project reads the same spool.

Delivery is never inferred (the mailbox's rule, kept): an entry is "filed" iff it carries an actual
``ticket_id``, never because time passed or because something displayed it.

APPEND-ONLY JSONL, deliberately. State is folded from a log of records rather than rewritten in
place, so a capture is a single ``open(..., "a")`` line write — no read-modify-write window in which
two concurrent sessions (or af-build workers) can clobber each other's request, and no partially
rewritten file if the process dies mid-write. A short trailing ``filed`` record retires an entry.

CLI (what the skill and the Stop hook actually call)::

    python3 rapid_queue.py capture "<request text>" [--project X] [--cwd DIR]
    python3 rapid_queue.py pending [--project X] [--cwd DIR]
    python3 rapid_queue.py filed <qid> <ticket-id> [--project X] [--cwd DIR]

Every subcommand prints ONE json object/array on stdout, so a caller parses it instead of scraping.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

# The helper modules (_gate_common) live next to this file. A bare hook subprocess may be launched
# with an arbitrary cwd, so make sure our own directory is importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _gate_common import active_project as _active_project  # noqa: E402

#: Spool root — ``AF_RAPID_QUEUE_DIR`` if set, else beside the MCP identity cache (``~/.praxis``).
#: Outside any repo or worktree ON PURPOSE: a queued request must outlive the checkout it was typed
#: in, must never show up in ``git status``, and must be visible to every af-build worker at once.
_DEFAULT_SPOOL_DIR = Path.home() / ".praxis" / "rapid-queue"

#: Record kinds in the log.
KIND_QUEUED = "queued"
KIND_FILED = "filed"

#: Retention for FILED records: they exist only as a local audit tail (the ticket itself lives in
#: Praxis), so compaction drops them once they are this old. Un-filed ``queued`` records are NEVER
#: dropped by age — that would be exactly the "lost a request" failure this file exists to prevent.
FILED_RETENTION_S = 7 * 24 * 3600

#: Compact opportunistically once the log crosses this size (a capture is otherwise pure append).
_COMPACT_AT_BYTES = 256 * 1024


# --------------------------------------------------------------------------- addressing

def bare_project(project: str | None = None, cwd: str | None = None) -> str:
    """The project's BARE name — the Praxis *space* name, which is also the spool's key.

    Resolution is delegated to :func:`_gate_common.active_project` (``FACTORY_PROJECT`` else the cwd
    basename) so the spool is keyed by exactly the same identity the Stop gates arm on; the
    ``prd-`` prefix that function adds is stripped back off, since the space is the bare name.
    """
    raw = (project or "").strip() or _active_project(cwd or os.getcwd())
    raw = raw.strip()
    return raw[len("prd-"):] if raw.startswith("prd-") else raw


def spool_path(project: str | None = None, cwd: str | None = None) -> Path:
    """The log file for ``project``. ``AF_RAPID_QUEUE_PATH`` pins one exact file (tests, and a
    caller that wants an explicit address); otherwise ``<spool dir>/<project>.jsonl``."""
    override = os.environ.get("AF_RAPID_QUEUE_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    root = os.environ.get("AF_RAPID_QUEUE_DIR", "").strip()
    directory = Path(root).expanduser() if root else _DEFAULT_SPOOL_DIR
    name = bare_project(project, cwd) or "unscoped"
    return directory / f"{name.replace('/', '-')}.jsonl"


# --------------------------------------------------------------------------- log I/O

def _read_records(path: Path) -> list[dict]:
    """Every record in the log, skipping any single unparseable line.

    A truncated final line (killed mid-append) must cost that one record at most — never the whole
    spool, which would discard requests that were written correctly.
    """
    if not path.exists():
        return []
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("qid"):
                records.append(record)
    return records


def _append(path: Path, record: dict) -> None:
    """Append ONE record. Single write of a single line — the whole atomicity story."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def fold(records: Iterable[dict]) -> list[dict]:
    """Fold the log into one entry per ``qid``, in capture order.

    A ``filed`` record merges its ``ticket_id``/``promoted_at`` onto the ``queued`` record it names.
    A ``filed`` record whose ``queued`` record is gone (compacted) contributes nothing — it cannot
    resurrect an entry as pending, which is what matters.
    """
    entries: dict[str, dict] = {}
    for record in records:
        qid = str(record["qid"])
        if record.get("kind") == KIND_QUEUED:
            entries.setdefault(qid, {}).update({
                "qid": qid,
                "text": record.get("text", ""),
                "posted_at": record.get("posted_at"),
                "cwd": record.get("cwd"),
                "project": record.get("project"),
                "promoted_at": None,
                "ticket_id": None,
            })
        elif record.get("kind") == KIND_FILED and qid in entries:
            # First filing wins — re-filing must not rewrite the ticket a request already became.
            if entries[qid].get("ticket_id") is None:
                entries[qid]["promoted_at"] = record.get("promoted_at")
                entries[qid]["ticket_id"] = record.get("ticket_id")
    return list(entries.values())


def status_of(entry: dict) -> str:
    """``"filed"`` iff the entry carries a real ticket id, else ``"queued"``. Never derived from
    elapsed time or from the fact that something displayed it (the mailbox's rule)."""
    return KIND_FILED if entry.get("ticket_id") else KIND_QUEUED


# --------------------------------------------------------------------------- public API

def capture(text: str, *, project: str | None = None, cwd: str | None = None,
            now: Optional[float] = None, qid: str | None = None) -> dict:
    """Record ``text`` as a pending request and return its entry. The FIRST thing af-rapid-queue
    does, before any Praxis call, so a failure after this point can only delay a request, not lose
    it. Refuses empty text (an empty capture is a bug, not a request)."""
    if not text.strip():
        raise ValueError("cannot capture an empty rapid-queue request")
    path = spool_path(project, cwd)
    record = {
        "kind": KIND_QUEUED,
        "qid": qid or uuid.uuid4().hex[:12],
        "text": text.strip(),
        "posted_at": now if now is not None else time.time(),
        "cwd": str(cwd or os.getcwd()),
        "project": bare_project(project, cwd),
    }
    _maybe_compact(path, now=record["posted_at"])
    _append(path, record)
    return {k: record[k] for k in ("qid", "text", "posted_at", "cwd", "project")} | {
        "promoted_at": None, "ticket_id": None,
    }


def entries(project: str | None = None, cwd: str | None = None) -> list[dict]:
    """Every entry known to this project's spool, filed and pending alike (capture order)."""
    return fold(_read_records(spool_path(project, cwd)))


def pending(project: str | None = None, cwd: str | None = None) -> list[dict]:
    """The entries still owed a Praxis ticket. This is what the Stop hook surfaces and what the
    skill promotes; it shrinks ONLY via :func:`mark_filed`."""
    return [entry for entry in entries(project, cwd) if status_of(entry) == KIND_QUEUED]


def mark_filed(qid: str, ticket_id: str, *, project: str | None = None, cwd: str | None = None,
               now: Optional[float] = None) -> dict:
    """Retire entry ``qid``: it is now Praxis ticket ``ticket_id``.

    Requires a non-empty ticket id — the one gate that keeps "retired" honest, since without it a
    caller could clear the spool without the request ever becoming work. Idempotent: re-filing an
    already-filed entry leaves the original ticket id in place. Raises ``KeyError`` for an unknown
    qid, so a typo surfaces instead of silently retiring nothing.
    """
    if not str(ticket_id).strip():
        raise ValueError(f"cannot file rapid-queue entry {qid!r} without a Praxis ticket id")
    path = spool_path(project, cwd)
    current = {entry["qid"]: entry for entry in fold(_read_records(path))}
    if qid not in current:
        raise KeyError(f"no rapid-queue entry {qid!r} in {path}")
    if status_of(current[qid]) == KIND_FILED:
        return current[qid]
    _append(path, {
        "kind": KIND_FILED,
        "qid": qid,
        "ticket_id": str(ticket_id).strip(),
        "promoted_at": now if now is not None else time.time(),
    })
    return current[qid] | {"ticket_id": str(ticket_id).strip()}


# --------------------------------------------------------------------------- compaction

def compact(path: Path, *, now: Optional[float] = None) -> int:
    """Rewrite ``path`` keeping every pending entry plus recently-filed ones; returns records kept.

    Pending entries are ALWAYS kept regardless of age. Written to a temp file and ``os.replace``d so
    a crash mid-compaction leaves the previous log intact.
    """
    stamp = now if now is not None else time.time()
    kept: list[dict] = []
    for entry in fold(_read_records(path)):
        if status_of(entry) == KIND_QUEUED:
            kept.append({k: entry[k] for k in ("qid", "text", "posted_at", "cwd", "project")}
                        | {"kind": KIND_QUEUED})
            continue
        promoted_at = entry.get("promoted_at") or 0
        if stamp - promoted_at <= FILED_RETENTION_S:
            kept.append({k: entry[k] for k in ("qid", "text", "posted_at", "cwd", "project")}
                        | {"kind": KIND_QUEUED})
            kept.append({"kind": KIND_FILED, "qid": entry["qid"],
                         "ticket_id": entry["ticket_id"], "promoted_at": promoted_at})
    tmp = path.with_suffix(path.suffix + ".compact")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8") as fh:
        for record in kept:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return len(kept)


def _maybe_compact(path: Path, *, now: Optional[float] = None) -> None:
    """Compact only once the log is big enough to bother, and never at the cost of a capture: any
    failure here is swallowed, because the append that follows is what must not be lost."""
    try:
        if path.exists() and path.stat().st_size > _COMPACT_AT_BYTES:
            compact(path, now=now)
    except OSError:
        pass


# --------------------------------------------------------------------------- CLI

def _emit(payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="af-rapid-queue capture spool")
    parser.add_argument("--project", default=None, help="bare project/space name (default: FACTORY_PROJECT or cwd)")
    parser.add_argument("--cwd", default=None, help="directory to resolve the project from")
    sub = parser.add_subparsers(dest="command", required=True)

    capture_cmd = sub.add_parser("capture", help="record a request (do this BEFORE any Praxis call)")
    capture_cmd.add_argument("text", help="the request, verbatim")

    sub.add_parser("pending", help="entries still owed a Praxis ticket")

    filed_cmd = sub.add_parser("filed", help="retire an entry that is now a Praxis ticket")
    filed_cmd.add_argument("qid")
    filed_cmd.add_argument("ticket_id")

    args = parser.parse_args(argv)
    scope = {"project": args.project, "cwd": args.cwd}

    if args.command == "capture":
        entry = capture(args.text, **scope)
        _emit({"captured": entry, "spool": str(spool_path(**scope)),
               "pending_count": len(pending(**scope))})
    elif args.command == "pending":
        _emit({"spool": str(spool_path(**scope)), "pending": pending(**scope)})
    elif args.command == "filed":
        _emit({"filed": mark_filed(args.qid, args.ticket_id, **scope)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
