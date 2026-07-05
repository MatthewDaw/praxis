"""Append-only structured event log — the factory's compounding spine.

Every run writes a JSONL file at ``runs/<run_id>/events.jsonl``. Each line is one
event carrying a monotonic ``seq`` and an optional ``parent_seq`` for happens-before
causality. The log is the local source of the outcome / episodic / derivation data
Praxis does not store (gaps H1/H4/H5); later milestones mine it for write-back.

Design rules:
- Append-only. Events are never edited or deleted; correction is a new event.
- Self-contained lines. Each line is independently parseable JSON (JSONL), so a
  partial/interrupted run is still replayable.
- Schema is a near one-way door — keep the core fields (``seq``, ``ts``, ``run_id``,
  ``type``) stable; put everything else in free-form fields.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: The closed vocabulary of event types. Extend deliberately — this is part of the
#: log's stable contract.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run_start",
        "run_end",
        "plan",
        "task_start",
        "task_end",
        "decision",
        "tool_call",
        "tool_result",
        "memory_read",   # a Praxis retrieval
        "memory_write",  # a Praxis insight/ingest
        "memory_audit",  # a rejected-pile / integrity check
        "gate_result",   # a verification gate outcome
        "outcome",       # final success/failure of a task or run
        "note",
    }
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventLog:
    """An append-only JSONL event log for one factory run.

    Re-opening an existing run resumes the sequence counter so appends continue
    monotonically (supports the disposable-agent / resume pattern in M2).
    """

    def __init__(self, run_id: str, root: str | Path = "runs") -> None:
        if not run_id or "/" in run_id or "\\" in run_id:
            raise ValueError(f"invalid run_id: {run_id!r}")
        self.run_id = run_id
        self.dir = Path(root) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "events.jsonl"
        self._lock = threading.Lock()
        self._seq = max((ev.get("seq", 0) for ev in self.read()), default=0)

    def append(self, type: str, *, parent_seq: int | None = None, **fields: Any) -> dict[str, Any]:
        """Append one event and return the written record (including its ``seq``)."""
        if type not in EVENT_TYPES:
            raise ValueError(
                f"unknown event type {type!r}; allowed: {sorted(EVENT_TYPES)}"
            )
        with self._lock:
            self._seq += 1
            event: dict[str, Any] = {
                "seq": self._seq,
                "ts": _utcnow_iso(),
                "run_id": self.run_id,
                "type": type,
            }
            if parent_seq is not None:
                event["parent_seq"] = parent_seq
            event.update(fields)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            return event

    def read(self) -> list[dict[str, Any]]:
        """Read all events for this run in order (empty list if none yet)."""
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    @property
    def last_seq(self) -> int:
        return self._seq
