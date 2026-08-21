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
    schema_version: int
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
        content = self.path.read_bytes()
        raw_lines = content.splitlines(keepends=True)
        if raw_lines and not raw_lines[-1].endswith((b"\n", b"\r")):
            self._quarantine_torn(raw_lines[-1], b"".join(raw_lines[:-1]))
            raw_lines = raw_lines[:-1]
        for number, raw_line in enumerate(raw_lines, 1):
            try:
                raw = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                if number == len(raw_lines):
                    self._quarantine_torn(raw_line, b"".join(raw_lines[:-1]))
                    break
                raise EventLogError(f"malformed event line {number}") from exc
            required = {"schema_version", "sequence", "event_type", "payload", "at", "previous_hash", "event_hash"}
            if not isinstance(raw, dict) or set(raw) != required:
                raise EventLogError(f"invalid event fields at line {number}")
            body = {key: raw[key] for key in required - {"event_hash"}}
            digest = hashlib.sha256(_canonical(body)).hexdigest()
            if raw["sequence"] != number or raw["previous_hash"] != previous or raw["event_hash"] != digest:
                raise EventLogError(f"event hash chain is broken at line {number}")
            if not isinstance(raw["payload"], dict) or not isinstance(raw["event_type"], str):
                raise EventLogError(f"invalid event payload at line {number}")
            if raw["schema_version"] != 1:
                raise EventLogError(f"unsupported event schema_version at line {number}")
            result.append(RegistryEvent(1, number, raw["event_type"], raw["payload"], float(raw["at"]),
                                        previous, digest))
            previous = digest
        return tuple(result)

    def append(self, event_type: str, payload: Mapping[str, Any], *, at: float) -> RegistryEvent:
        events = self.read()
        body = {"schema_version": 1, "sequence": len(events) + 1, "event_type": event_type, "payload": dict(payload),
                "at": float(at), "previous_hash": events[-1].event_hash if events else None}
        raw = {**body, "event_hash": hashlib.sha256(_canonical(body)).hexdigest()}
        with self.path.open("ab") as handle:
            handle.write(_canonical(raw) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        return RegistryEvent(1, raw["sequence"], event_type, dict(payload), float(at),
                             raw["previous_hash"], raw["event_hash"])

    def _quarantine_torn(self, torn: bytes, intact: bytes) -> None:
        quarantine = self.path.with_name(
            f"{self.path.name}.torn-{hashlib.sha256(torn).hexdigest()[:16]}"
        )
        quarantine.write_bytes(torn)
        with self.path.open("wb") as handle:
            handle.write(intact)
            handle.flush()
            os.fsync(handle.fileno())
