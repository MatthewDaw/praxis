from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping


class EventLogError(ValueError):
    pass


@dataclass(frozen=True)
class RegistryEvent:
    sequence: int
    event_type: str
    payload: Mapping[str, Any]
    at: float
    previous_hash: str | None
    event_hash: str


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


class EventLog:
    """Append-only, fsynced and hash-chained JSONL audit log."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> tuple[RegistryEvent, ...]:
        if not self.path.exists():
            return ()
        result: list[RegistryEvent] = []
        previous: str | None = None
        for number, line in enumerate(self.path.read_text().splitlines(), 1):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EventLogError(f"malformed event line {number}") from exc
            required = {"sequence", "event_type", "payload", "at", "previous_hash", "event_hash"}
            if not isinstance(raw, dict) or set(raw) != required:
                raise EventLogError(f"invalid event fields at line {number}")
            body = {key: raw[key] for key in required - {"event_hash"}}
            digest = hashlib.sha256(_canonical(body)).hexdigest()
            if raw["sequence"] != number or raw["previous_hash"] != previous or raw["event_hash"] != digest:
                raise EventLogError(f"event hash chain is broken at line {number}")
            if not isinstance(raw["payload"], dict) or not isinstance(raw["event_type"], str):
                raise EventLogError(f"invalid event payload at line {number}")
            result.append(RegistryEvent(number, raw["event_type"], raw["payload"], float(raw["at"]),
                                        previous, digest))
            previous = digest
        return tuple(result)

    def append(self, event_type: str, payload: Mapping[str, Any], *, at: float) -> RegistryEvent:
        events = self.read()
        body = {"sequence": len(events) + 1, "event_type": event_type, "payload": dict(payload),
                "at": float(at), "previous_hash": events[-1].event_hash if events else None}
        raw = {**body, "event_hash": hashlib.sha256(_canonical(body)).hexdigest()}
        with self.path.open("ab") as handle:
            handle.write(_canonical(raw) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        return RegistryEvent(raw["sequence"], event_type, dict(payload), float(at),
                             raw["previous_hash"], raw["event_hash"])
