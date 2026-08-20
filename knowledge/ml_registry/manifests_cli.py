"""JSON CLI for immutable dataset, split, and prediction manifests."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from knowledge.ml_registry.manifests import (
    DatasetFile,
    DatasetManifest,
    GroupAssignment,
    ManifestRegistry,
    ManifestValidationError,
    PredictionManifest,
    SplitManifest,
)
from knowledge.ml_registry.file_lock import exclusive_file_lock

EXIT_MALFORMED_INPUT = 2
EXIT_VALIDATION_ERROR = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage immutable ML data manifests")
    parser.add_argument("--file", required=True, help="manifest registry JSON file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    for command in ("add-dataset", "add-split", "add-prediction"):
        child = commands.add_parser(command)
        child.add_argument("--spec", required=True, help="JSON specification file")
    show = commands.add_parser("show")
    show.add_argument("--kind", choices=("dataset", "split", "prediction"))
    show.add_argument("--id")
    commands.add_parser("validate")
    return parser


def _load(path: Path, allow_new: bool = False) -> ManifestRegistry:
    if path.exists():
        return ManifestRegistry.load(path)
    if allow_new:
        return ManifestRegistry(path)
    raise ManifestValidationError(f"manifest registry {str(path)!r} does not exist")


def _mutate(
    path: Path,
    operation: Callable[[ManifestRegistry], Any],
    *,
    allow_new: bool = False,
) -> Any:
    with exclusive_file_lock(path):
        registry = _load(path, allow_new)
        result = operation(registry)
        registry.save()
        return result


def _spec(path: str) -> dict[str, Any]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("manifest specification must be a JSON object")
    return document


def _dataset(document: dict[str, Any]) -> DatasetManifest:
    values = dict(document)
    try:
        values["files"] = [DatasetFile(**item) for item in values["files"]]
        return DatasetManifest.create(**values)
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid dataset specification: {exc}") from exc


def _split(document: dict[str, Any]) -> SplitManifest:
    values = dict(document)
    try:
        values["assignments"] = [GroupAssignment(**item) for item in values["assignments"]]
        return SplitManifest.create(**values)
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid split specification: {exc}") from exc


def _prediction(document: dict[str, Any]) -> PredictionManifest:
    try:
        return PredictionManifest.create(**document)
    except TypeError as exc:
        raise ValueError(f"invalid prediction specification: {exc}") from exc


def _document(registry: ManifestRegistry) -> dict[str, Any]:
    return {
        "schema_version": registry.SCHEMA_VERSION,
        "datasets": [asdict(item) for _, item in sorted(registry.datasets.items())],
        "splits": [asdict(item) for _, item in sorted(registry.splits.items())],
        "predictions": [asdict(item) for _, item in sorted(registry.predictions.items())],
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.file)
    if args.command == "init":
        if path.exists():
            raise ManifestValidationError(f"manifest registry {str(path)!r} already exists")
        _mutate(path, lambda registry: None, allow_new=True)
        return {"ok": True, "file": str(path), "schema_version": ManifestRegistry.SCHEMA_VERSION}
    if args.command == "add-dataset":
        manifest = _dataset(_spec(args.spec))
        stored = _mutate(path, lambda registry: registry.add_dataset(manifest), allow_new=True)
        return {"ok": True, "kind": "dataset", "manifest": asdict(stored)}
    if args.command == "add-split":
        manifest = _split(_spec(args.spec))
        stored = _mutate(path, lambda registry: registry.add_split(manifest))
        return {"ok": True, "kind": "split", "manifest": asdict(stored)}
    if args.command == "add-prediction":
        manifest = _prediction(_spec(args.spec))
        stored = _mutate(path, lambda registry: registry.add_prediction(manifest))
        return {"ok": True, "kind": "prediction", "manifest": asdict(stored)}

    registry = _load(path)
    if args.command == "validate":
        registry.validate()
        return {
            "ok": True,
            "dataset_count": len(registry.datasets),
            "split_count": len(registry.splits),
            "prediction_count": len(registry.predictions),
        }
    if args.command == "show":
        if args.id and not args.kind:
            raise ManifestValidationError("--id requires --kind")
        if not args.kind:
            return {"ok": True, "registry": _document(registry)}
        collection = {
            "dataset": registry.datasets,
            "split": registry.splits,
            "prediction": registry.predictions,
        }[args.kind]
        if args.id:
            try:
                return {"ok": True, "kind": args.kind, "manifest": asdict(collection[args.id])}
            except KeyError as exc:
                raise ManifestValidationError(
                    f"unknown {args.kind} manifest {args.id!r}"
                ) from exc
        return {
            "ok": True,
            "kind": args.kind,
            "manifests": [asdict(item) for _, item in sorted(collection.items())],
        }
    raise ValueError(f"unknown command {args.command!r}")  # pragma: no cover


def main(argv: list[str] | None = None) -> int:
    try:
        print(json.dumps(run(_parser().parse_args(argv)), sort_keys=True))
        return 0
    except ManifestValidationError as exc:
        print(json.dumps({"ok": False, "error": "validation", "message": str(exc)}))
        return EXIT_VALIDATION_ERROR
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
        # Persisted state is untrusted input: a malformed document must refuse with a
        # documented exit code, never escape as a traceback.
        print(json.dumps({"ok": False, "error": "malformed_input", "message": str(exc)}))
        return EXIT_MALFORMED_INPUT


if __name__ == "__main__":
    sys.exit(main())
