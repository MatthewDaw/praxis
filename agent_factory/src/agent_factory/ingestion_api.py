"""The ingestion API — the sole writer of validation/knowledge content (FL1 / KD1 / KD3).

FL1 lays the foundation KD1 and R1's later ingestion sequence build on: a shared org-level
``factory-learnings`` space holds lessons (and, eventually, the failure-class taxonomy), it is
cloud-canonical (Praxis, never a local file), and it is mounted READ-ONLY into every project
space at claim/resolve time (see ``hooks._ticket_state.start_ticket``). This module is the ONLY
place in the codebase that writes into that space — :func:`write_lesson` is the sole write path.
Reads (:func:`read_lessons`) are plain GETs, which nothing can turn into a write, so any project
session that wants to see a lesson goes through them (or the read-only mount) and never through a
write endpoint.

The full ingestion SEQUENCE (classify/dedup, draft a check, attempt fail-then-pass proof, bind,
activate, regress matching tickets — R1/R2) lands with FL2 onward; this module carries only the
lesson read/write primitives and the CLI shell those later tickets extend.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from hooks import _praxis

LESSON_CATEGORY = "lesson"
CLASS_CATEGORY = "failure-class"          # FL3 — the failure-class taxonomy (R3)
CALIBRATION_CATEGORY = "taxonomy-calibration"  # FL3 — the R20b staged-rollout singleton state


def _write_insight(text: str, category: str, *, source: str | None = None,
                   meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Shared write path: POST /insights, scoped to the shared ``factory-learnings`` space.

    THE sole write path into that space (R1/KD3): nothing else in this codebase is allowed to
    target ``(hooks._praxis.FACTORY_LEARNINGS_SPACE, hooks._praxis.FACTORY_LEARNINGS_SNAPSHOT)``
    with a write. Idempotently bootstraps the space on first use (a space that has never been
    created 404s on its first snapshot-bound write). Returns the server's insight-write ack
    (``{"summary","action","id",...}``).
    """
    body = str(text or "").strip()
    if not body:
        raise ValueError("text is required")
    _praxis.ensure_space(_praxis.FACTORY_LEARNINGS_SPACE, name="factory-learnings")
    payload: dict[str, Any] = {
        "insight": body,
        "category": category,
        "source": source,
        "meta": meta or {},
    }
    return _praxis._request(
        "POST", "/insights", body=payload,
        space=_praxis.FACTORY_LEARNINGS_SPACE, snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT,
    )


def write_lesson(text: str, *, source: str | None = None,
                 meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write ``text`` as a lesson into the shared ``factory-learnings`` space (POST /insights)."""
    return _write_insight(text, LESSON_CATEGORY, source=source, meta=meta)


def read_lessons(query: str = "", *, top_k: int = 10) -> list[dict[str, Any]]:
    """Read-only lessons lookup against the shared ``factory-learnings`` space.

    A GET-only call (``context``/``facts_by``): there is no write side-effect, so this is safe to
    call from any project session without granting it a write path. With a non-empty ``query`` this
    is similarity-ranked (top ``top_k``); with an empty query it is the exhaustive active-lesson
    enumeration.
    """
    q = (query or "").strip()
    if q:
        return _praxis.context(q, top_k=top_k, space=_praxis.FACTORY_LEARNINGS_SPACE,
                               snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)
    return _praxis.facts_by(category=LESSON_CATEGORY, space=_praxis.FACTORY_LEARNINGS_SPACE,
                            snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)


# --------------------------------------------------------------------------- FL3 taxonomy + calibration
# The dedup MATCHING logic and calibration MATH live in ``agent_factory.failure_taxonomy`` — this
# module stays the sole writer/reader of the shared space; that module never touches ``_praxis``.

def write_class(label: str, *, source: str | None = None,
                meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Mint a new failure-class fact (R3: a genuinely novel failure)."""
    return _write_insight(label, CLASS_CATEGORY, source=source, meta=meta)


def read_classes() -> list[dict[str, Any]]:
    """Read-only enumeration of every failure class (GET-only, no write side-effect)."""
    return _praxis.facts_by(category=CLASS_CATEGORY, space=_praxis.FACTORY_LEARNINGS_SPACE,
                            snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)


def update_class_meta(class_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Merge ``meta`` onto an existing class fact (recurrence count + evidence log) — a recurrence
    NEVER writes a duplicate lesson, it only updates the class it matched."""
    return _praxis.patch_meta(class_id, meta, space=_praxis.FACTORY_LEARNINGS_SPACE,
                              snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)


def read_calibration_state() -> dict[str, Any] | None:
    """The singleton R20b calibration-state fact, or ``None`` before the first assignment ever
    recorded. Read-only."""
    hits = _praxis.facts_by(category=CALIBRATION_CATEGORY, space=_praxis.FACTORY_LEARNINGS_SPACE,
                            snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)
    return hits[0] if hits else None


def write_calibration_state(meta: dict[str, Any]) -> dict[str, Any]:
    """Create-or-update the singleton calibration-state fact. First call mints it; every later call
    merges the new counters onto the same fact, so there is always exactly one."""
    existing = read_calibration_state()
    if existing is not None:
        return _praxis.patch_meta(existing["id"], meta, space=_praxis.FACTORY_LEARNINGS_SPACE,
                                  snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)
    return _write_insight("failure-class taxonomy calibration state", CALIBRATION_CATEGORY, meta=meta)


def _cmd_ingest(args: argparse.Namespace) -> int:
    result = write_lesson(args.text, source=args.source)
    print(result.get("summary") or result.get("id") or "ok")
    return 0


def _cmd_read(args: argparse.Namespace) -> int:
    for hit in read_lessons(args.query, top_k=args.top_k):
        text = str(hit.get("text") or "")
        print(f"{hit.get('id', '')}\t{text}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="agent_factory.ingestion_api",
        description="The sole writer of the shared factory-learnings space (FL1). "
                    "The full classify/dedup/draft/proof/bind/activate sequence (R1) lands with FL2+.")
    sub = ap.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="write a lesson into the factory-learnings space")
    ingest.add_argument("text", help="the lesson text")
    ingest.add_argument("--source", default=None, help="provenance pointer for the lesson")
    ingest.set_defaults(func=_cmd_ingest)

    read = sub.add_parser("read", help="read lessons from the factory-learnings space (read-only)")
    read.add_argument("query", nargs="?", default="", help="similarity query; omit for all lessons")
    read.add_argument("--top-k", type=int, default=10, dest="top_k")
    read.set_defaults(func=_cmd_read)

    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
