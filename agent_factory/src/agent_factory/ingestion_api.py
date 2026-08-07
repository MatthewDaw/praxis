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


def write_lesson(text: str, *, source: str | None = None,
                 meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write ``text`` as a lesson into the shared ``factory-learnings`` space (POST /insights).

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
        "category": LESSON_CATEGORY,
        "source": source,
        "meta": meta or {},
    }
    return _praxis._request(
        "POST", "/insights", body=payload,
        space=_praxis.FACTORY_LEARNINGS_SPACE, snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT,
    )


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
