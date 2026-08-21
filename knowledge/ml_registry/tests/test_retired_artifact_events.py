from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from knowledge.ml_registry.compatibility import LegacyEventError, read_retired_event_log


def _write(root: Path, sequence: int, event_type: str, payload: dict, previous: str | None) -> str:
    document = {
        "schema_version": 1, "event_id": f"event-{sequence}", "sequence": sequence,
        "event_type": event_type, "occurred_at": float(sequence), "payload": payload,
        "previous_event_sha256": previous,
    }
    unsigned = json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    document["event_sha256"] = hashlib.sha256(unsigned).hexdigest()
    path = root / "events" / f"{sequence:020d}-event-{sequence}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n")
    return document["event_sha256"]


def test_retired_event_log_remains_authenticatable_without_becoming_a_write_authority(tmp_path: Path):
    first = _write(tmp_path, 1, "artifact_ingested", {"artifact": {"id": "a"}}, None)
    _write(tmp_path, 2, "campaign_finalized", {"promotion": {"id": "p"}}, first)
    events = read_retired_event_log(tmp_path)
    assert [(event.sequence, event.event_type) for event in events] == [
        (1, "artifact_ingested"), (2, "campaign_finalized"),
    ]
    assert events[0].payload == {"artifact": {"id": "a"}}
    assert not hasattr(events[0], "save")


def test_retired_event_decoder_fails_closed_on_tampering(tmp_path: Path):
    _write(tmp_path, 1, "artifact_ingested", {"artifact": {"id": "a"}}, None)
    path = next((tmp_path / "events").iterdir())
    path.write_text(path.read_text().replace('"id":"a"', '"id":"b"'))
    with pytest.raises(LegacyEventError, match="hash does not verify"):
        read_retired_event_log(tmp_path)
