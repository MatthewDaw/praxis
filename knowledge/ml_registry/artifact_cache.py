"""Immutable metadata index for shared model artifacts and prediction caches."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Mapping

from knowledge.ml_registry.schema import RegistryValidationError


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CacheKey:
    upstream_fit_id: str
    upstream_artifact_id: str
    dataset_manifest: str
    split: str
    preprocessing: str
    feature_schema: str

    @property
    def id(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True)
class CacheEntry:
    entry_id: str
    key: CacheKey
    uri: str
    checksum: str
    coverage: float
    prediction_scope: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryValidationError(
            f"{field} must be a non-empty string", field=field
        )
    return value.strip()


def key_from_dict(raw: Mapping[str, object]) -> CacheKey:
    fields = tuple(CacheKey.__dataclass_fields__)
    values = {name: _text(raw.get(name), name) for name in fields}
    return CacheKey(**values)


def _entry_from_dict(raw: Mapping[str, object]) -> CacheEntry:
    key_raw = raw.get("key")
    if not isinstance(key_raw, Mapping):
        raise RegistryValidationError("entry key must be an object", field="key")
    return CacheEntry(
        entry_id=_text(raw.get("entry_id"), "entry_id"),
        key=key_from_dict(key_raw),
        uri=_text(raw.get("uri"), "uri"),
        checksum=_text(raw.get("checksum"), "checksum"),
        coverage=float(raw.get("coverage", -1)),
        prediction_scope=_text(raw.get("prediction_scope"), "prediction_scope"),
    )


class ArtifactCacheIndex:
    """Index whose entries never change or disappear; only active pointers move."""

    def __init__(self) -> None:
        self.entries: dict[str, CacheEntry] = {}
        self.active: dict[str, str] = {}
        self.superseded: dict[str, str] = {}

    def register(
        self,
        key: CacheKey,
        *,
        uri: str,
        checksum: str,
        coverage: float,
        prediction_scope: str,
    ) -> CacheEntry:
        uri = _text(uri, "uri")
        checksum = _text(checksum, "checksum")
        if (
            isinstance(coverage, bool)
            or not math.isfinite(coverage)
            or not 0 <= coverage <= 1
        ):
            raise RegistryValidationError(
                "coverage must be finite and within [0, 1]", field="coverage"
            )
        coverage = float(coverage)
        if prediction_scope not in {"oof", "in_fold", "not_predictions"}:
            raise RegistryValidationError(
                "prediction_scope must be oof, in_fold, or not_predictions",
                field="prediction_scope",
            )
        payload = {
            "key": asdict(key),
            "uri": uri,
            "checksum": checksum,
            "coverage": coverage,
            "prediction_scope": prediction_scope,
        }
        entry = CacheEntry(
            _digest(payload), key, uri, checksum, coverage, prediction_scope
        )
        existing = self.entries.get(entry.entry_id)
        if existing is not None:
            return existing
        old = self.active.get(key.id)
        self.entries[entry.entry_id] = entry
        self.active[key.id] = entry.entry_id
        if old is not None and old != entry.entry_id:
            self.superseded[old] = entry.entry_id
        return entry

    def lookup(
        self,
        key: CacheKey,
        *,
        require_oof: bool = False,
        minimum_coverage: float = 0.0,
        expected_checksum: str | None = None,
    ) -> CacheEntry:
        entry_id = self.active.get(key.id)
        if entry_id is None:
            raise RegistryValidationError(
                "no active cache entry matches the exact lineage key", field="key"
            )
        entry = self.entries[entry_id]
        if require_oof and entry.prediction_scope != "oof":
            raise RegistryValidationError(
                "in-fold predictions cannot be reused where out-of-fold predictions are required",
                field="prediction_scope",
            )
        if not 0 <= minimum_coverage <= 1:
            raise RegistryValidationError(
                "minimum_coverage must be within [0, 1]", field="minimum_coverage"
            )
        if entry.coverage < minimum_coverage:
            raise RegistryValidationError(
                f"cache coverage {entry.coverage} is below required {minimum_coverage}",
                field="coverage",
            )
        if expected_checksum is not None and entry.checksum != expected_checksum:
            raise RegistryValidationError(
                "cache checksum does not match the expected checksum", field="checksum"
            )
        return entry

    def invalidate(self, entry_id: str, *, reason: str) -> None:
        if entry_id not in self.entries:
            raise RegistryValidationError(
                f"unknown cache entry {entry_id!r}", field="entry_id"
            )
        _text(reason, "reason")
        entry = self.entries[entry_id]
        if self.active.get(entry.key.id) == entry_id:
            self.active.pop(entry.key.id)
        self.superseded[entry_id] = f"invalidated:{reason}"

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "entries": {key: entry.to_dict() for key, entry in self.entries.items()},
            "active": self.active,
            "superseded": self.superseded,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "ArtifactCacheIndex":
        index = cls()
        entries = raw.get("entries", {})
        if not isinstance(entries, Mapping):
            raise RegistryValidationError("entries must be an object", field="entries")
        for entry_id, value in entries.items():
            if not isinstance(value, Mapping):
                raise RegistryValidationError(
                    "cache entry must be an object", field="entries"
                )
            entry = _entry_from_dict(value)
            if (
                entry.entry_id != entry_id
                or _digest(
                    {
                        "key": asdict(entry.key),
                        "uri": entry.uri,
                        "checksum": entry.checksum,
                        "coverage": entry.coverage,
                        "prediction_scope": entry.prediction_scope,
                    }
                )
                != entry.entry_id
            ):
                raise RegistryValidationError(
                    "cache entry content hash does not verify", field="entry_id"
                )
            index.entries[entry.entry_id] = entry
        for name, target in (
            ("active", index.active),
            ("superseded", index.superseded),
        ):
            value = raw.get(name, {})
            if not isinstance(value, Mapping):
                raise RegistryValidationError(f"{name} must be an object", field=name)
            target.update({str(k): str(v) for k, v in value.items()})
        return index


def load_index(path: Path) -> ArtifactCacheIndex:
    if not path.exists():
        return ArtifactCacheIndex()
    raw = json.loads(path.read_text())
    if not isinstance(raw, Mapping):
        raise RegistryValidationError(
            "cache index must be a JSON object", field="index"
        )
    return ArtifactCacheIndex.from_dict(raw)


def save_index(path: Path, index: ArtifactCacheIndex) -> None:
    """Atomically replace the index; blob contents remain at their external URI."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(index.to_dict(), stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _object(raw: str) -> dict[str, object]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("expected an inline JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Metadata-only shared artifact cache index"
    )
    parser.add_argument("--index", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register")
    register.add_argument("--key-json", required=True)
    register.add_argument("--uri", required=True)
    register.add_argument("--checksum", required=True)
    register.add_argument("--coverage", required=True, type=float)
    register.add_argument("--prediction-scope", required=True)
    lookup = sub.add_parser("lookup")
    lookup.add_argument("--key-json", required=True)
    lookup.add_argument("--require-oof", action="store_true")
    lookup.add_argument("--minimum-coverage", type=float, default=0)
    lookup.add_argument("--expected-checksum")
    invalidate = sub.add_parser("invalidate")
    invalidate.add_argument("--entry-id", required=True)
    invalidate.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    path = Path(args.index)
    try:
        index = load_index(path)
        if args.command == "register":
            result = index.register(
                key_from_dict(_object(args.key_json)),
                uri=args.uri,
                checksum=args.checksum,
                coverage=args.coverage,
                prediction_scope=args.prediction_scope,
            )
            save_index(path, index)
            print(json.dumps(result.to_dict(), indent=2))
        elif args.command == "lookup":
            result = index.lookup(
                key_from_dict(_object(args.key_json)),
                require_oof=args.require_oof,
                minimum_coverage=args.minimum_coverage,
                expected_checksum=args.expected_checksum,
            )
            print(json.dumps(result.to_dict(), indent=2))
        else:
            index.invalidate(args.entry_id, reason=args.reason)
            save_index(path, index)
            print(json.dumps({"status": "invalidated", "entry_id": args.entry_id}))
        return 0
    except (RegistryValidationError, ValueError, OSError, json.JSONDecodeError) as exc:
        field = getattr(exc, "field", "input")
        print(
            json.dumps({"status": "refused", "field": field, "reason": str(exc)}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
