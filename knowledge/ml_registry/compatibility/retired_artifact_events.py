"""Read and authenticate the retired JSON artifact-event log without reviving it.

The former store is not a write authority and its domain records do not map losslessly to
the standard registry: they lack an Experiment, typed Run metrics/code_ref, and the inputs
needed to create a ModelVersion honestly.  This decoder therefore preserves authenticated
historical bytes as tombstones for an explicit archive import; it never writes Registry.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping


class LegacyEventError(ValueError):
    """A frozen legacy event cannot be authenticated exactly."""


@dataclass(frozen=True)
class LegacyEventTombstone:
    sequence: int
    event_id: str
    event_type: str
    occurred_at: float
    payload: Mapping[str, Any]
    previous_event_sha256: str | None
    event_sha256: str
    source_path: str


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _digest(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json({
        key: value for key, value in document.items() if key != "event_sha256"
    })).hexdigest()


def _decode(path: Path, expected_sequence: int, previous: str | None) -> LegacyEventTombstone:
    try:
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyEventError(f"invalid retired event {path}: {exc}") from exc
    required = {
        "schema_version", "event_id", "sequence", "event_type", "occurred_at", "payload",
        "previous_event_sha256", "event_sha256",
    }
    if not isinstance(document, dict) or set(document) != required:
        raise LegacyEventError(f"retired event fields do not match schema at {path}")
    if document["schema_version"] != 1 or document["sequence"] != expected_sequence:
        raise LegacyEventError(f"retired event schema or sequence drift at {path}")
    expected_name = f"{expected_sequence:020d}-{document['event_id']}.json"
    if path.name != expected_name or document["previous_event_sha256"] != previous:
        raise LegacyEventError(f"retired event filename or hash-chain drift at {path}")
    if _digest(document) != document["event_sha256"]:
        raise LegacyEventError(f"retired event hash does not verify at {path}")
    if document["event_type"] not in {"artifact_ingested", "campaign_finalized"}:
        raise LegacyEventError(f"unsupported retired event type at {path}")
    if not isinstance(document["payload"], dict):
        raise LegacyEventError(f"retired event payload must be an object at {path}")
    occurred = document["occurred_at"]
    if isinstance(occurred, bool) or not isinstance(occurred, (int, float)) or not math.isfinite(occurred):
        raise LegacyEventError(f"retired event occurred_at must be finite at {path}")
    return LegacyEventTombstone(
        sequence=expected_sequence,
        event_id=document["event_id"],
        event_type=document["event_type"],
        occurred_at=float(occurred),
        payload=document["payload"],
        previous_event_sha256=previous,
        event_sha256=document["event_sha256"],
        source_path=str(path),
    )


def read_retired_event_log(root: str | Path) -> tuple[LegacyEventTombstone, ...]:
    """Authenticate a frozen event directory and return read-only migration tombstones."""
    events_path = Path(root) / "events"
    paths = sorted(events_path.glob("*.json")) if events_path.exists() else []
    events: list[LegacyEventTombstone] = []
    previous: str | None = None
    for sequence, path in enumerate(paths, 1):
        event = _decode(path, sequence, previous)
        events.append(event)
        previous = event.event_sha256
    return tuple(events)
