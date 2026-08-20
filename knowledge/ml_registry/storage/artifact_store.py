"""Immutable, content-addressed campaign artifacts and their event history.

The event stream is authoritative.  JSON projections are disposable views rebuilt from it,
including after a process dies between committing an event and replacing a projection.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping
import uuid

from knowledge.ml_registry.contracts.artifact_manifest import CampaignArtifact
from knowledge.ml_registry.contracts._validation import ContractError
from knowledge.ml_registry.file_lock import exclusive_file_lock


class ArtifactStoreError(ValueError):
    """The immutable store is corrupt or a requested write conflicts with history."""


ProjectionBuilder = Callable[["ArtifactSnapshot"], Mapping[str, Any]]


@dataclass(frozen=True)
class ArtifactEvent:
    schema_version: int
    event_id: str
    sequence: int
    event_type: str
    occurred_at: float
    payload: Mapping[str, Any]
    previous_event_sha256: str | None
    event_sha256: str


@dataclass(frozen=True)
class ArtifactSnapshot:
    events: tuple[ArtifactEvent, ...]
    artifacts: Mapping[str, CampaignArtifact]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")


def _event_digest(document: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in document.items() if key != "event_sha256"}
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class ArtifactStore:
    """One append-only artifact authority with rebuildable JSON projections."""

    EVENT_SCHEMA_VERSION = 1

    def __init__(
        self,
        root: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        projection_builders: Mapping[str, ProjectionBuilder] | None = None,
    ) -> None:
        self.root = Path(root)
        self.events_path = self.root / "events"
        self.blobs_path = self.root / "blobs" / "sha256"
        self.projections_path = self.root / "projections"
        self._lock_target = self.root / "store"
        self._clock = clock
        builders = dict(projection_builders or {})
        for name in builders:
            if not name or Path(name).name != name or name in {".", ".."}:
                raise ArtifactStoreError(f"invalid projection name {name!r}")
        self._projection_builders = MappingProxyType(builders)

    def ingest_artifact(
        self, source_path: str | Path, artifact: CampaignArtifact,
    ) -> CampaignArtifact:
        """Verify and durably ingest one blob; identical retries append no event."""
        source = Path(source_path)
        with exclusive_file_lock(self._lock_target):
            snapshot = self._replay_unlocked()
            blob_path = self._blob_path(artifact.sha256)
            canonical = replace(artifact, uri=blob_path.resolve().as_uri())
            existing = snapshot.artifacts.get(artifact.artifact_id)
            if existing is not None:
                if existing != canonical:
                    raise ArtifactStoreError(
                        f"artifact id {artifact.artifact_id!r} is immutable and its content drifted"
                    )
                self._verify_blob(existing)
                self._rebuild_projections_unlocked(snapshot)
                return existing

            self._copy_verified_blob(source, artifact.sha256, artifact.size_bytes, blob_path)
            event = self._new_event(
                snapshot, "artifact_ingested", {"artifact": canonical.to_mapping()},
            )
            self._write_event(event)
            updated = self._replay_unlocked()
            self._rebuild_projections_unlocked(updated)
            return canonical

    def replay(self) -> ArtifactSnapshot:
        """Validate and fold the complete event hash chain."""
        with exclusive_file_lock(self._lock_target):
            return self._replay_unlocked()

    def artifact(self, artifact_id: str) -> CampaignArtifact:
        snapshot = self.replay()
        try:
            return snapshot.artifacts[artifact_id]
        except KeyError as exc:
            raise ArtifactStoreError(f"unknown artifact {artifact_id!r}") from exc

    def verify_artifact(self, artifact_id: str) -> CampaignArtifact:
        artifact = self.artifact(artifact_id)
        self._verify_blob(artifact)
        return artifact

    def rebuild_projections(self) -> ArtifactSnapshot:
        """Replay authoritative events and atomically replace every configured view."""
        with exclusive_file_lock(self._lock_target):
            snapshot = self._replay_unlocked()
            self._rebuild_projections_unlocked(snapshot)
            return snapshot

    def _blob_path(self, digest: str) -> Path:
        return self.blobs_path / digest[:2] / digest

    def _copy_verified_blob(
        self, source: Path, expected_digest: str, expected_size: int, destination: Path,
    ) -> None:
        if not source.is_file():
            raise ArtifactStoreError(f"artifact source is not a file: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=destination.parent,
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
                while chunk := incoming.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            actual_digest = digest.hexdigest()
            if actual_digest != expected_digest:
                raise ArtifactStoreError(
                    f"artifact sha256 mismatch: expected {expected_digest}, got {actual_digest}"
                )
            if size != expected_size:
                raise ArtifactStoreError(
                    f"artifact size mismatch: expected {expected_size}, got {size}"
                )
            if destination.exists():
                self._verify_path(destination, expected_digest, expected_size)
            else:
                os.replace(temporary, destination)
                _fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _verify_blob(self, artifact: CampaignArtifact) -> None:
        self._verify_path(self._blob_path(artifact.sha256), artifact.sha256, artifact.size_bytes)

    @staticmethod
    def _verify_path(path: Path, expected_digest: str, expected_size: int) -> None:
        if not path.is_file():
            raise ArtifactStoreError(f"artifact blob is missing: {path}")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        if digest.hexdigest() != expected_digest or size != expected_size:
            raise ArtifactStoreError(f"artifact blob failed checksum or size verification: {path}")

    def _new_event(
        self, snapshot: ArtifactSnapshot, event_type: str, payload: Mapping[str, Any],
    ) -> ArtifactEvent:
        previous = snapshot.events[-1].event_sha256 if snapshot.events else None
        document: dict[str, Any] = {
            "schema_version": self.EVENT_SCHEMA_VERSION,
            "event_id": uuid.uuid4().hex,
            "sequence": len(snapshot.events) + 1,
            "event_type": event_type,
            "occurred_at": float(self._clock()),
            "payload": dict(payload),
            "previous_event_sha256": previous,
        }
        document["event_sha256"] = _event_digest(document)
        return self._event_from_document(document)

    def _write_event(self, event: ArtifactEvent) -> None:
        document = self._event_to_document(event)
        path = self.events_path / f"{event.sequence:020d}-{event.event_id}.json"
        if path.exists():
            raise ArtifactStoreError(f"event path already exists: {path}")
        _atomic_bytes(path, _canonical_json(document) + b"\n")

    def _replay_unlocked(self) -> ArtifactSnapshot:
        event_paths = sorted(self.events_path.glob("*.json")) if self.events_path.exists() else []
        events: list[ArtifactEvent] = []
        artifacts: dict[str, CampaignArtifact] = {}
        previous: str | None = None
        for expected_sequence, path in enumerate(event_paths, 1):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ArtifactStoreError(f"invalid artifact event {path}: {exc}") from exc
            if not isinstance(document, Mapping):
                raise ArtifactStoreError(f"artifact event {path} must be a JSON object")
            event = self._event_from_document(document)
            expected_name = f"{event.sequence:020d}-{event.event_id}.json"
            if path.name != expected_name or event.sequence != expected_sequence:
                raise ArtifactStoreError(f"artifact event sequence or filename drift at {path}")
            if event.previous_event_sha256 != previous:
                raise ArtifactStoreError(f"artifact event hash chain is broken at {path}")
            if _event_digest(document) != event.event_sha256:
                raise ArtifactStoreError(f"artifact event hash does not verify at {path}")
            if event.event_type != "artifact_ingested":
                raise ArtifactStoreError(f"unsupported artifact event type {event.event_type!r}")
            raw_artifact = event.payload.get("artifact")
            if not isinstance(raw_artifact, Mapping):
                raise ArtifactStoreError("artifact_ingested event has no artifact object")
            try:
                artifact = CampaignArtifact.from_mapping(raw_artifact)
            except ContractError as exc:
                raise ArtifactStoreError(f"invalid artifact contract in event {path}: {exc}") from exc
            existing = artifacts.get(artifact.artifact_id)
            if existing is not None and existing != artifact:
                raise ArtifactStoreError(
                    f"artifact id {artifact.artifact_id!r} has conflicting immutable events"
                )
            artifacts[artifact.artifact_id] = artifact
            events.append(event)
            previous = event.event_sha256
        return ArtifactSnapshot(tuple(events), MappingProxyType(artifacts))

    def _rebuild_projections_unlocked(self, snapshot: ArtifactSnapshot) -> None:
        for name, builder in self._projection_builders.items():
            document = builder(snapshot)
            if not isinstance(document, Mapping):
                raise ArtifactStoreError(f"projection {name!r} did not return an object")
            _atomic_bytes(
                self.projections_path / f"{name}.json",
                json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n",
            )

    @classmethod
    def _event_from_document(cls, document: Mapping[str, Any]) -> ArtifactEvent:
        required = {
            "schema_version", "event_id", "sequence", "event_type", "occurred_at", "payload",
            "previous_event_sha256", "event_sha256",
        }
        if set(document) != required:
            raise ArtifactStoreError("artifact event fields do not match schema")
        if document.get("schema_version") != cls.EVENT_SCHEMA_VERSION:
            raise ArtifactStoreError("unsupported artifact event schema_version")
        if isinstance(document.get("sequence"), bool) or not isinstance(document.get("sequence"), int):
            raise ArtifactStoreError("artifact event sequence must be an integer")
        payload = document.get("payload")
        if not isinstance(payload, Mapping):
            raise ArtifactStoreError("artifact event payload must be an object")
        for field in ("event_id", "event_type", "event_sha256"):
            if not isinstance(document.get(field), str) or not document[field]:
                raise ArtifactStoreError(f"artifact event {field} must be non-empty text")
        occurred_at = document.get("occurred_at")
        if (isinstance(occurred_at, bool) or not isinstance(occurred_at, (int, float))
                or not math.isfinite(float(occurred_at))):
            raise ArtifactStoreError("artifact event occurred_at must be finite numeric")
        previous = document.get("previous_event_sha256")
        if previous is not None and not isinstance(previous, str):
            raise ArtifactStoreError("previous_event_sha256 must be text or null")
        return ArtifactEvent(
            schema_version=cls.EVENT_SCHEMA_VERSION,
            event_id=document["event_id"],
            sequence=document["sequence"],
            event_type=document["event_type"],
            occurred_at=float(occurred_at),
            payload=MappingProxyType(dict(payload)),
            previous_event_sha256=previous,
            event_sha256=document["event_sha256"],
        )

    @staticmethod
    def _event_to_document(event: ArtifactEvent) -> dict[str, Any]:
        return {
            "schema_version": event.schema_version,
            "event_id": event.event_id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at,
            "payload": dict(event.payload),
            "previous_event_sha256": event.previous_event_sha256,
            "event_sha256": event.event_sha256,
        }
