"""Immutable, content-addressed manifests for campaign data and predictions."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


class ManifestValidationError(ValueError):
    """A manifest is malformed, leaks groups, or has drifted from its hash."""


def _canonical(value: Any) -> Any:
    """Collapse representations that mean the same number so hashes cannot drift.

    ``1`` and ``1.0`` are the same JSON value to every consumer of these manifests, so
    they must hash identically; non-finite floats have no RFC 8259 representation and
    are refused outright rather than serialised as ``NaN``/``Infinity``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ManifestValidationError("manifest values must be finite numbers")
        return int(value) if value.is_integer() else value
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _hash(kind: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        _canonical({"kind": kind, **payload}), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _count(value: Any, name: str) -> int:
    """A count or size is an ``int``; ``True`` and ``1.5`` are not counts."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestValidationError(f"{name} must be an integer")
    return value


def _nonempty_mapping(value: dict[str, Any], name: str) -> None:
    if not isinstance(value, dict) or not value:
        raise ManifestValidationError(f"{name} must be a non-empty JSON object")


@dataclass(frozen=True)
class DatasetFile:
    identity: str
    checksum: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not self.identity or not self.checksum:
            raise ManifestValidationError("dataset file identity and checksum must be non-empty")
        if not isinstance(self.identity, str) or not isinstance(self.checksum, str):
            raise ManifestValidationError("dataset file identity and checksum must be strings")
        _count(self.size_bytes, "dataset file size_bytes")
        if self.size_bytes < 0:
            raise ManifestValidationError("dataset file size_bytes cannot be negative")


@dataclass(frozen=True)
class DatasetManifest:
    id: str
    files: tuple[DatasetFile, ...]
    schema: dict[str, Any]
    provenance: dict[str, Any]
    hash: str

    @classmethod
    def create(
        cls,
        manifest_id: str,
        files: Iterable[DatasetFile],
        schema: dict[str, Any],
        provenance: dict[str, Any],
    ) -> "DatasetManifest":
        if not manifest_id:
            raise ManifestValidationError("dataset manifest id must be non-empty")
        records = tuple(sorted(files, key=lambda item: item.identity))
        if not records:
            raise ManifestValidationError("dataset manifest must contain at least one file")
        identities = [record.identity for record in records]
        if len(identities) != len(set(identities)):
            raise ManifestValidationError("dataset manifest contains duplicate file identity")
        _nonempty_mapping(schema, "dataset schema")
        _nonempty_mapping(provenance, "dataset provenance")
        payload = {
            "id": manifest_id,
            "files": [asdict(record) for record in records],
            "schema": schema,
            "provenance": provenance,
        }
        return cls(manifest_id, records, schema, provenance, _hash("dataset", payload))


#: The closed split vocabulary, in strict temporal order.  A campaign that needs
#: another split name must extend this list deliberately rather than by typo.
SPLIT_ORDER: tuple[str, ...] = ("train", "calibration", "validation", "test")
SPLIT_RANK: dict[str, int] = {name: index for index, name in enumerate(SPLIT_ORDER)}
#: Splits whose groups must lie strictly after everything a fold trained on.
EVALUATION_SPLITS = frozenset(SPLIT_ORDER[1:])


@dataclass(frozen=True)
class GroupAssignment:
    group_id: str
    split: str
    temporal_order: int

    def __post_init__(self) -> None:
        if not isinstance(self.group_id, str) or not self.group_id:
            raise ManifestValidationError("group_id must be a non-empty string")
        if self.split not in SPLIT_RANK:
            raise ManifestValidationError(
                f"unknown split {self.split!r}; split must be one of {list(SPLIT_ORDER)}"
            )
        if isinstance(self.temporal_order, bool) or not isinstance(self.temporal_order, int):
            raise ManifestValidationError("temporal_order must be an integer")


@dataclass(frozen=True)
class SplitManifest:
    id: str
    dataset_manifest_hash: str
    assignments: tuple[GroupAssignment, ...]
    hash: str

    @classmethod
    def create(
        cls,
        manifest_id: str,
        dataset_manifest_hash: str,
        assignments: Iterable[GroupAssignment],
    ) -> "SplitManifest":
        if not manifest_id or not dataset_manifest_hash:
            raise ManifestValidationError("split id and dataset_manifest_hash must be non-empty")
        records = tuple(sorted(assignments, key=lambda item: (item.group_id, item.split)))
        if not records:
            raise ManifestValidationError("split manifest must assign at least one group")
        seen: dict[str, str] = {}
        for record in records:
            if record.group_id in seen:
                raise ManifestValidationError(
                    f"duplicate group leakage: group {record.group_id!r} appears in "
                    f"{seen[record.group_id]!r} and {record.split!r}"
                )
            seen[record.group_id] = record.split
        if len(set(seen.values())) < 2:
            raise ManifestValidationError("split manifest must contain at least two disjoint splits")
        ranks: dict[str, list[int]] = {}
        for record in records:
            ranks.setdefault(record.split, []).append(record.temporal_order)
        present = [name for name in SPLIT_ORDER if name in ranks]
        for earlier, later in zip(present, present[1:]):
            if max(ranks[earlier]) >= min(ranks[later]):
                raise ManifestValidationError(
                    f"temporal leakage: {earlier} must precede {later}"
                )
        payload = {
            "id": manifest_id,
            "dataset_manifest_hash": dataset_manifest_hash,
            "assignments": [asdict(record) for record in records],
        }
        return cls(manifest_id, dataset_manifest_hash, records, _hash("split", payload))

    def groups(self, split: str) -> frozenset[str]:
        return frozenset(item.group_id for item in self.assignments if item.split == split)

    def assert_disjoint(self, *splits: str) -> None:
        selected = splits or tuple(sorted({item.split for item in self.assignments}))
        for index, left in enumerate(selected):
            for right in selected[index + 1:]:
                overlap = self.groups(left) & self.groups(right)
                if overlap:
                    raise ManifestValidationError(
                        f"group leakage between {left!r} and {right!r}: {sorted(overlap)!r}"
                    )


@dataclass(frozen=True)
class PredictionManifest:
    id: str
    upstream_artifact_id: str
    split_manifest_hash: str
    predicted_count: int
    eligible_count: int
    coverage: float
    schema: dict[str, Any]
    group_coverage: dict[str, float]
    out_of_fold: bool
    fold_id_by_group: dict[str, str]
    training_groups_by_fold: dict[str, list[str]]
    hash: str

    @classmethod
    def create(
        cls,
        manifest_id: str,
        upstream_artifact_id: str,
        split_manifest_hash: str,
        *,
        predicted_count: int,
        eligible_count: int,
        coverage: float,
        schema: dict[str, Any],
        group_coverage: dict[str, float],
        out_of_fold: bool,
        fold_id_by_group: dict[str, str],
        training_groups_by_fold: dict[str, list[str]],
    ) -> "PredictionManifest":
        for name, value in (("id", manifest_id), ("upstream_artifact_id", upstream_artifact_id),
                            ("split_manifest_hash", split_manifest_hash)):
            if not isinstance(value, str) or not value:
                raise ManifestValidationError(
                    "prediction id, upstream_artifact_id, and split_manifest_hash must be "
                    f"non-empty strings ({name} was {value!r})"
                )
        _count(predicted_count, "predicted_count")
        _count(eligible_count, "eligible_count")
        if isinstance(coverage, bool) or not isinstance(coverage, (int, float)):
            raise ManifestValidationError("prediction coverage must be numeric")
        if not math.isfinite(float(coverage)):
            raise ManifestValidationError("prediction coverage must be finite")
        if not isinstance(out_of_fold, bool):
            raise ManifestValidationError("out_of_fold must be a boolean")
        if not isinstance(fold_id_by_group, dict):
            raise ManifestValidationError("fold_id_by_group must be a JSON object")
        if not isinstance(training_groups_by_fold, dict):
            raise ManifestValidationError("training_groups_by_fold must be a JSON object")
        if eligible_count <= 0 or predicted_count < 0 or predicted_count > eligible_count:
            raise ManifestValidationError("prediction counts must satisfy 0 <= predicted <= eligible")
        if not 0.0 <= coverage <= 1.0:
            raise ManifestValidationError("prediction coverage must be between 0 and 1")
        measured = predicted_count / eligible_count
        if abs(measured - coverage) > 1e-12:
            raise ManifestValidationError(
                f"prediction coverage {coverage:g} does not match counts ({measured:g})"
            )
        _nonempty_mapping(schema, "prediction schema")
        _nonempty_mapping(group_coverage, "prediction group_coverage")
        for group, value in group_coverage.items():
            if not group or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
                raise ManifestValidationError(
                    f"group coverage for {group!r} must be between 0 and 1"
                )
        if not out_of_fold:
            raise ManifestValidationError("prediction manifest must guarantee out_of_fold=true")
        groups = set(group_coverage)
        if set(fold_id_by_group) != groups:
            raise ManifestValidationError("fold proof must assign every predicted group")
        for group, fold in fold_id_by_group.items():
            if not isinstance(group, str) or not group or not isinstance(fold, str) or not fold:
                raise ManifestValidationError("fold_id_by_group must map group ids to fold ids")
            training = training_groups_by_fold.get(fold)
            if not isinstance(training, list) or group in training:
                raise ManifestValidationError(
                    f"fold proof does not exclude predicted group {group!r} from training"
                )
        for fold, training in training_groups_by_fold.items():
            if not isinstance(fold, str) or not fold:
                raise ManifestValidationError("training_groups_by_fold keys must be fold ids")
            if not isinstance(training, list) or not training:
                raise ManifestValidationError(
                    f"fold {fold!r} must declare a non-empty training group list"
                )
            if not all(isinstance(item, str) and item for item in training):
                raise ManifestValidationError(
                    f"fold {fold!r} training groups must all be non-empty strings"
                )
            if len(set(training)) != len(training):
                raise ManifestValidationError(f"fold {fold!r} repeats a training group")
        if len(training_groups_by_fold) < 2:
            raise ManifestValidationError(
                "out-of-fold proof requires at least two folds"
            )
        payload = {
            "id": manifest_id,
            "upstream_artifact_id": upstream_artifact_id,
            "split_manifest_hash": split_manifest_hash,
            "predicted_count": predicted_count,
            "eligible_count": eligible_count,
            "coverage": coverage,
            "schema": schema,
            "group_coverage": group_coverage,
            "out_of_fold": out_of_fold,
            "fold_id_by_group": fold_id_by_group,
            "training_groups_by_fold": training_groups_by_fold,
        }
        return cls(
            manifest_id, upstream_artifact_id, split_manifest_hash, predicted_count,
            eligible_count, coverage, schema, group_coverage, out_of_fold,
            fold_id_by_group, training_groups_by_fold,
            _hash("prediction", payload),
        )


class ManifestRegistry:
    """An immutable manifest catalog with atomic JSON persistence."""

    SCHEMA_VERSION = 2

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.datasets: dict[str, DatasetManifest] = {}
        self.splits: dict[str, SplitManifest] = {}
        self.predictions: dict[str, PredictionManifest] = {}

    def add_dataset(self, manifest: DatasetManifest) -> DatasetManifest:
        return self._add(self.datasets, manifest)

    def add_split(self, manifest: SplitManifest) -> SplitManifest:
        dataset_hashes = {item.hash for item in self.datasets.values()}
        if manifest.dataset_manifest_hash not in dataset_hashes:
            raise ManifestValidationError("split references an unknown dataset manifest hash")
        manifest.assert_disjoint()
        return self._add(self.splits, manifest)

    def add_prediction(self, manifest: PredictionManifest) -> PredictionManifest:
        split = next(
            (item for item in self.splits.values() if item.hash == manifest.split_manifest_hash),
            None,
        )
        if split is None:
            raise ManifestValidationError("prediction references an unknown split manifest hash")
        self._validate_prediction_groups(manifest, split)
        return self._add(self.predictions, manifest)

    @staticmethod
    def _validate_prediction_groups(
        manifest: PredictionManifest, split: SplitManifest
    ) -> None:
        expected = {item.group_id for item in split.assignments}
        actual = set(manifest.group_coverage)
        if actual != expected:
            missing, extra = sorted(expected - actual), sorted(actual - expected)
            raise ManifestValidationError(
                f"prediction group_coverage does not match split groups; "
                f"missing={missing!r}, extra={extra!r}"
            )
        unreferenced = sorted(
            set(manifest.training_groups_by_fold) - set(manifest.fold_id_by_group.values())
        )
        if unreferenced:
            raise ManifestValidationError(
                f"training_groups_by_fold declares folds no group is predicted by: {unreferenced!r}"
            )
        temporal = {item.group_id: item.temporal_order for item in split.assignments}
        split_of = {item.group_id: item.split for item in split.assignments}
        held_out: dict[str, set[str]] = {}
        for group, fold in manifest.fold_id_by_group.items():
            held_out.setdefault(fold, set()).add(group)
        for fold, training in manifest.training_groups_by_fold.items():
            unknown = sorted(set(training) - expected)
            if unknown:
                raise ManifestValidationError(
                    f"fold {fold!r} trains on groups absent from the split: {unknown!r}"
                )
            latest = max(temporal[group] for group in training)
            for group in sorted(held_out.get(fold, set())):
                if split_of[group] in EVALUATION_SPLITS and latest >= temporal[group]:
                    raise ManifestValidationError(
                        f"temporal leakage: fold {fold!r} trained through order {latest} but "
                        f"predicts {split_of[group]} group {group!r} at order {temporal[group]}"
                    )

    @staticmethod
    def _add(collection: dict[str, Any], manifest: Any) -> Any:
        existing = collection.get(manifest.id)
        if existing is not None:
            if existing.hash != manifest.hash:
                raise ManifestValidationError(
                    f"hash drift for immutable manifest {manifest.id!r}: "
                    f"{existing.hash} != {manifest.hash}"
                )
            return existing
        collection[manifest.id] = manifest
        return manifest

    def validate(self) -> None:
        for manifest in self.datasets.values():
            rebuilt = DatasetManifest.create(
                manifest.id, manifest.files, manifest.schema, manifest.provenance
            )
            self._assert_hash(manifest, rebuilt)
        for manifest in self.splits.values():
            rebuilt = SplitManifest.create(
                manifest.id, manifest.dataset_manifest_hash, manifest.assignments
            )
            self._assert_hash(manifest, rebuilt)
            if manifest.dataset_manifest_hash not in {item.hash for item in self.datasets.values()}:
                raise ManifestValidationError("split references an unknown dataset manifest hash")
        for manifest in self.predictions.values():
            rebuilt = PredictionManifest.create(
                manifest.id,
                manifest.upstream_artifact_id,
                manifest.split_manifest_hash,
                predicted_count=manifest.predicted_count,
                eligible_count=manifest.eligible_count,
                coverage=manifest.coverage,
                schema=manifest.schema,
                group_coverage=manifest.group_coverage,
                out_of_fold=manifest.out_of_fold,
                fold_id_by_group=manifest.fold_id_by_group,
                training_groups_by_fold=manifest.training_groups_by_fold,
            )
            self._assert_hash(manifest, rebuilt)
            split = next(
                (item for item in self.splits.values() if item.hash == manifest.split_manifest_hash),
                None,
            )
            if split is None:
                raise ManifestValidationError("prediction references an unknown split manifest hash")
            self._validate_prediction_groups(manifest, split)

    @staticmethod
    def _assert_hash(stored: Any, rebuilt: Any) -> None:
        if stored.hash != rebuilt.hash:
            raise ManifestValidationError(
                f"hash drift for immutable manifest {stored.id!r}: "
                f"stored {stored.hash}, computed {rebuilt.hash}"
            )

    def save(self) -> None:
        if self.path is None:
            raise ManifestValidationError("cannot save a manifest registry without a path")
        self.validate()
        document = {
            "schema_version": self.SCHEMA_VERSION,
            "datasets": [asdict(value) for _, value in sorted(self.datasets.items())],
            "splits": [asdict(value) for _, value in sorted(self.splits.items())],
            "predictions": [asdict(value) for _, value in sorted(self.predictions.items())],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, sort_keys=True, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @classmethod
    def load(cls, path: str | Path) -> "ManifestRegistry":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestValidationError(f"invalid manifest registry: {exc}") from exc
        if not isinstance(document, dict):
            raise ManifestValidationError("manifest registry must be a JSON object")
        version = document.get("schema_version")
        if version != cls.SCHEMA_VERSION:
            if isinstance(version, int) and version < cls.SCHEMA_VERSION:
                raise ManifestValidationError(
                    f"manifest registry schema_version {version} predates version "
                    f"{cls.SCHEMA_VERSION}; migrate the document by re-creating its split "
                    "assignments with an explicit temporal_order and a known split name"
                )
            raise ManifestValidationError("unsupported manifest registry schema_version")
        registry = cls(path)
        try:
            for name in ("datasets", "splits", "predictions"):
                if not isinstance(document.get(name, []), list):
                    raise TypeError(f"{name} must be an array")
            for item in document.get("datasets", []):
                raw = dict(item)
                raw["files"] = tuple(DatasetFile(**entry) for entry in raw["files"])
                manifest = DatasetManifest(**raw)
                registry.datasets[manifest.id] = manifest
            for item in document.get("splits", []):
                raw = dict(item)
                raw["assignments"] = tuple(GroupAssignment(**entry) for entry in raw["assignments"])
                manifest = SplitManifest(**raw)
                registry.splits[manifest.id] = manifest
            for item in document.get("predictions", []):
                manifest = PredictionManifest(**dict(item))
                registry.predictions[manifest.id] = manifest
            registry.validate()
        except ManifestValidationError:
            raise
        except (TypeError, KeyError, ValueError, AttributeError, ArithmeticError) as exc:
            raise ManifestValidationError(f"malformed manifest registry: {exc}") from exc
        return registry
