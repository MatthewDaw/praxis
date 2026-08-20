"""One deterministic semantic fixture rendered through all three legacy artifact views."""

from __future__ import annotations

import json
from pathlib import Path

from knowledge.ml_registry.artifact_cache import ArtifactCacheIndex, CacheKey, save_index
from knowledge.ml_registry.manifests import (
    DatasetFile,
    DatasetManifest,
    GroupAssignment,
    ManifestRegistry,
    PredictionManifest,
    SplitManifest,
)
from knowledge.ml_registry.portfolio import Portfolio


FIXED_CREATED_AT = "2026-08-20T12:34:56+00:00"


def render_legacy_artifact_views(root: Path) -> dict[str, bytes]:
    """Persist byte-exact legacy views derived from a single shared lineage."""
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest_registry.json"
    registry = ManifestRegistry(manifest_path)
    dataset = registry.add_dataset(DatasetManifest.create(
        "dataset-canonical",
        (
            DatasetFile("game-b.mp4", "sha256:game-b", 20),
            DatasetFile("game-a.mp4", "sha256:game-a", 10),
        ),
        {"frame": "uint8[H,W,3]", "label": "bool"},
        {"commit": "0123456789abcdef", "source": "fixture://canonical"},
    ))
    split = registry.add_split(SplitManifest.create(
        "split-canonical", dataset.hash,
        (
            GroupAssignment("game-b", "validation", 2),
            GroupAssignment("game-a", "train", 1),
        ),
    ))
    prediction = registry.add_prediction(PredictionManifest.create(
        "prediction-canonical", "artifact-weights-v1", split.hash,
        predicted_count=18, eligible_count=20, coverage=.9,
        schema={"probability": "float32", "sample_id": "string"},
        group_coverage={"game-a": .9, "game-b": .9}, out_of_fold=True,
        fold_id_by_group={"game-a": "fold-a", "game-b": "fold-b"},
        training_groups_by_fold={"fold-a": ["game-b"], "fold-b": ["game-a"]},
    ))
    registry.save()

    cache_path = root / "artifact_cache_index.json"
    cache = ArtifactCacheIndex()
    cache.register(
        CacheKey(
            "fit-canonical", "artifact-weights-v1", dataset.hash, split.hash,
            "preprocess-v1", "features-v1",
        ),
        uri="file:///canonical/predictions.parquet", checksum="sha256:predictions",
        coverage=prediction.coverage, prediction_scope="oof",
    )
    save_index(cache_path, cache)

    portfolio_path = root / "portfolio_artifacts.json"
    portfolio = Portfolio(portfolio_path)
    artifact = portfolio.register_artifact(
        "artifact-weights-v1", "model-canonical", verdict="adopted",
        dataset_manifest_hash=dataset.hash, split_manifest_hash=split.hash,
        prediction_manifest_hash=prediction.hash, coverage=prediction.coverage,
    )
    artifact.created_at = FIXED_CREATED_AT
    portfolio.save()

    return {
        "manifest_registry": manifest_path.read_bytes(),
        "artifact_cache_index": cache_path.read_bytes(),
        "portfolio_artifacts": portfolio_path.read_bytes(),
    }


def canonical_json(view: bytes) -> object:
    """Parse helper used only for cross-view semantic assertions."""
    return json.loads(view)
