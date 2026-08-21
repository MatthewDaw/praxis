"""One deterministic semantic fixture rendered through all three legacy artifact views."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import subprocess

from knowledge.ml_registry.manifests import (
    DatasetFile,
    DatasetManifest,
    GroupAssignment,
    PredictionManifest,
    SplitManifest,
)
from knowledge.ml_registry.services.registry_aliases import adopt_run_and_promote
from knowledge.ml_registry.services.registry_runs import complete_run
from knowledge.ml_registry.storage import (
    LegacyArtifactDependency,
    LegacyCampaignProjection,
    PortfolioProjectionSpec,
    Registry,
    SIDECAR_SCHEMA,
    canonical_json_bytes,
    project_artifact_cache_index,
    project_manifest_registry,
    project_portfolio_artifacts,
)


FIXED_CREATED_AT = "2026-08-20T12:34:56+00:00"


def render_legacy_artifact_views(root: Path, *, include_history: bool = False) -> dict[str, bytes]:
    """Render byte-exact legacy views from one canonical Registry lineage."""
    dataset = DatasetManifest.create(
        "dataset-canonical",
        (
            DatasetFile("game-b.mp4", "sha256:game-b", 20),
            DatasetFile("game-a.mp4", "sha256:game-a", 10),
        ),
        {"frame": "uint8[H,W,3]", "label": "bool"},
        {"commit": "0123456789abcdef", "source": "fixture://canonical"},
    )
    split = SplitManifest.create(
        "split-canonical", dataset.hash,
        (
            GroupAssignment("game-b", "validation", 2),
            GroupAssignment("game-a", "train", 1),
        ),
    )
    prediction = PredictionManifest.create(
        "prediction-canonical", "artifact-weights-v1", split.hash,
        predicted_count=18, eligible_count=20, coverage=.9,
        schema={"probability": "float32", "sample_id": "string"},
        group_coverage={"game-a": .9, "game-b": .9}, out_of_fold=True,
        fold_id_by_group={"game-a": "fold-a", "game-b": "fold-b"},
        training_groups_by_fold={"fold-a": ["game-b"], "fold-b": ["game-a"]},
    )

    repo = Path(__file__).resolve().parents[3]
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    created_at = datetime.fromisoformat(FIXED_CREATED_AT).timestamp()
    registry = Registry(root / "canonical_registry", clock=lambda: created_at)
    registry.create_experiment(
        experiment_id="campaign-canonical", spec_digest="d" * 64,
        stages=["representation"], metric="score", direction="maximize",
        win_condition={"metric_at_least": .9}, noise_floor=.01,
        baseline_throughput=1.0,
    )
    registry.create_run(
        run_id="fit-canonical", experiment_id="campaign-canonical", idea_id="idea-canonical",
        stage="representation", family="canonical", params={}, metrics={},
        code_ref={"schema_version": 1, "repo": str(repo), "sha": sha, "base_sha": sha,
                  "diff_hash": "f" * 64, "diff_lines": 0},
        device_fingerprint="cpu:fixture", status="running", verdict=None,
        started_at=created_at, finished_at=None, claim_owner="fixture",
        heartbeat_at=created_at,
    )
    complete_run(registry, run_id="fit-canonical", metrics={
        "metric": .9, "validity": "valid", "throughput": 1.0,
        "throughput_unit": "rows_per_second", "memory_gb": 0.0,
        "cpu_time": 0.0, "load": {"start_1m": 0.0, "end_1m": 0.0},
    })
    registry.register_model(
        model_id="model-canonical", family="canonical", sport_scope="shared",
        axis="fixture", protocol="Fixture", extends=None,
    )
    registry.create_artifact(
        run_id="fit-canonical", kind="dataset_manifest",
        content=canonical_json_bytes(asdict(dataset)), schema_version="legacy-manifest/v1",
    )
    registry.create_artifact(
        run_id="fit-canonical", kind="split_manifest",
        content=canonical_json_bytes(asdict(split)), schema_version="legacy-manifest/v1",
    )
    registry.create_artifact(
        run_id="fit-canonical", kind="oof_predictions",
        content=canonical_json_bytes(asdict(prediction)), schema_version="legacy-manifest/v1",
    )
    checkpoint = registry.create_artifact(
        run_id="fit-canonical", kind="checkpoint", content=b"canonical weights",
        schema_version="1",
    )
    projection = {"schema_version": 1, "artifact": {
        "id": "artifact-weights-v1", "dataset_manifest_hash": dataset.hash,
        "split_manifest_hash": split.hash, "prediction_manifest_hash": prediction.hash,
        "coverage": prediction.coverage,
    },
        "cache": {
            "key": {
                "upstream_fit_id": "fit-canonical",
                "upstream_artifact_id": "artifact-weights-v1",
                "dataset_manifest": dataset.hash,
                "split": split.hash,
                "preprocessing": "preprocess-v1",
                "feature_schema": "features-v1",
            },
            "uri": "file:///canonical/predictions.parquet",
            "checksum": "sha256:predictions",
            "coverage": prediction.coverage,
            "prediction_scope": "oof",
        },
    }
    registry.create_artifact(
        run_id="fit-canonical", kind="report", content=canonical_json_bytes(projection),
        schema_version=SIDECAR_SCHEMA,
    )
    adopt_run_and_promote(
        registry, run_id="fit-canonical", model_id="model-canonical", reason="fixture",
        model_version={
            "version": 1, "artifact_id": checkpoint, "checksum": checkpoint,
            "family_version": "canonical@1", "code_sha": sha,
            "preprocessing_hash": "preprocess-v1",
            "calibration": {}, "thresholds": {},
            "compat_result": {"head_sha": sha, "passed": True, "at": created_at},
            "status": "active",
        },
    )
    if include_history:
        registry.clock = lambda: created_at + 60
        registry.create_run(
            run_id="fit-canonical-v2", experiment_id="campaign-canonical",
            idea_id="idea-canonical-v2", stage="representation", family="canonical",
            params={}, metrics={}, code_ref={
                "schema_version": 1, "repo": str(repo), "sha": sha, "base_sha": sha,
                "diff_hash": "e" * 64, "diff_lines": 0,
            }, device_fingerprint="cpu:fixture", status="running", verdict=None,
            started_at=created_at + 60, finished_at=None, claim_owner="fixture",
            heartbeat_at=created_at + 60,
        )
        complete_run(registry, run_id="fit-canonical-v2", metrics={
            "metric": .91, "validity": "valid", "throughput": 1.0,
            "throughput_unit": "rows_per_second", "memory_gb": 0.0,
            "cpu_time": 0.0, "load": {"start_1m": 0.0, "end_1m": 0.0},
        })
        successor = json.loads(json.dumps(projection))
        successor["artifact"]["id"] = "artifact-weights-v2"
        successor["cache"]["key"]["upstream_fit_id"] = "fit-canonical-v2"
        successor["cache"]["key"]["upstream_artifact_id"] = "artifact-weights-v2"
        successor["cache"]["uri"] = "file:///canonical/predictions-v2.parquet"
        successor["cache"]["checksum"] = "sha256:predictions-v2"
        registry.create_artifact(
            run_id="fit-canonical-v2", kind="report",
            content=canonical_json_bytes(successor), schema_version=SIDECAR_SCHEMA,
        )
        checkpoint_v2 = registry.create_artifact(
            run_id="fit-canonical-v2", kind="checkpoint", content=b"canonical weights v2",
            schema_version="1",
        )
        adopt_run_and_promote(
            registry, run_id="fit-canonical-v2", model_id="model-canonical", reason="fixture v2",
            model_version={
                "version": 2, "artifact_id": checkpoint_v2, "checksum": checkpoint_v2,
                "family_version": "canonical@1", "code_sha": sha,
                "preprocessing_hash": "preprocess-v1", "calibration": {}, "thresholds": {},
                "compat_result": {"head_sha": sha, "passed": True, "at": created_at + 60},
                "status": "active",
            },
        )
    portfolio_spec = PortfolioProjectionSpec(1, (
        LegacyCampaignProjection("consumer-canonical", "model-consumer", (
            LegacyArtifactDependency(
                "model-canonical", "artifact-weights-v1", "adopted", dataset.hash,
                split.hash, prediction.hash, .9,
            ),
        )),
    ))

    return {
        "manifest_registry": canonical_json_bytes(project_manifest_registry(registry)),
        "artifact_cache_index": canonical_json_bytes(project_artifact_cache_index(registry)),
        "portfolio_artifacts": canonical_json_bytes(project_portfolio_artifacts(
            registry, portfolio_spec=portfolio_spec,
        )),
    }


def canonical_json(view: bytes) -> object:
    """Parse helper used only for cross-view semantic assertions."""
    return json.loads(view)
