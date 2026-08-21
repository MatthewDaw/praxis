"""Immutable manifest hashing, leakage guards, and persistence."""

from __future__ import annotations

import pytest

from knowledge.ml_registry.manifests import (
    DatasetFile,
    DatasetManifest,
    GroupAssignment,
    ManifestRegistry,
    ManifestValidationError,
    PredictionManifest,
    SplitManifest,
)


def _dataset(manifest_id="matches"):
    return DatasetManifest.create(
        manifest_id,
        [DatasetFile("b.mp4", "sha-b", 20), DatasetFile("a.mp4", "sha-a", 10)],
        {"frame": "uint8[H,W,3]"},
        {"source": "research archive", "version": 1},
    )


def _split(dataset_hash):
    return SplitManifest.create(
        "match-split",
        dataset_hash,
        [GroupAssignment("match-b", "validation", 2), GroupAssignment("match-a", "train", 1)],
    )


def _prediction(split_hash, **overrides):
    values = {
        "predicted_count": 90,
        "eligible_count": 100,
        "coverage": 0.9,
        "schema": {"track_id": "string", "probability": "float32"},
        "group_coverage": {"match-a": 0.9, "match-b": 0.9},
        "out_of_fold": True,
        "fold_id_by_group": {"match-a": "fold-a", "match-b": "fold-b"},
        "training_groups_by_fold": {"fold-a": ["match-b"], "fold-b": ["match-a"]},
    }
    values.update(overrides)
    return PredictionManifest.create("tracking-oof", "tracking-fit-v1", split_hash, **values)


def test_dataset_hash_is_stable_across_file_order_but_changes_with_identity():
    first = _dataset()
    reordered = DatasetManifest.create(
        "matches",
        reversed(first.files),
        dict(first.schema),
        dict(first.provenance),
    )
    changed = DatasetManifest.create(
        "matches",
        [DatasetFile("a.mp4", "new-checksum", 10), DatasetFile("b.mp4", "sha-b", 20)],
        first.schema,
        first.provenance,
    )

    assert first.hash == reordered.hash
    assert first.hash != changed.hash
    assert [item.identity for item in first.files] == ["a.mp4", "b.mp4"]


def test_duplicate_file_identity_and_group_leakage_are_refused():
    with pytest.raises(ManifestValidationError, match="duplicate file identity"):
        DatasetManifest.create(
            "bad", [DatasetFile("same", "a", 1), DatasetFile("same", "b", 2)],
            {"x": "int"}, {"source": "x"},
        )
    with pytest.raises(ManifestValidationError, match="duplicate group leakage"):
        SplitManifest.create(
            "bad-split", "data-hash",
            [GroupAssignment("match-a", "train", 1), GroupAssignment("match-a", "validation", 2)],
        )


def test_split_requires_real_disjoint_partitions():
    with pytest.raises(ManifestValidationError, match="at least two"):
        SplitManifest.create(
            "one", "data-hash",
            [GroupAssignment("match-a", "train", 1), GroupAssignment("match-b", "train", 2)],
        )


def test_split_refuses_temporal_leakage_and_oof_requires_fold_proof():
    with pytest.raises(ManifestValidationError, match="temporal leakage"):
        SplitManifest.create(
            "leaky", "data-hash",
            [GroupAssignment("future-train", "train", 3),
             GroupAssignment("past-test", "test", 2)],
        )
    with pytest.raises(ManifestValidationError, match="does not exclude"):
        _prediction(
            "split", training_groups_by_fold={
                "fold-a": ["match-a", "match-b"], "fold-b": ["match-a"]
            },
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"coverage": 1.1}, "between 0 and 1"),
        ({"coverage": 0.8}, "does not match counts"),
        ({"out_of_fold": False}, "out_of_fold=true"),
        ({"group_coverage": {"match-a": -0.1}}, "between 0 and 1"),
    ],
)
def test_prediction_manifest_refuses_invalid_coverage_and_in_fold_output(overrides, message):
    with pytest.raises(ManifestValidationError, match=message):
        _prediction("split-hash", **overrides)


def test_registry_persists_and_reloads_complete_lineage(tmp_path):
    from knowledge.ml_registry.storage.registry import Registry
    from knowledge.ml_registry.tests.artifact_projection_fixture import render_legacy_artifact_views

    render_legacy_artifact_views(tmp_path)
    loaded = ManifestRegistry.from_registry(Registry(tmp_path / "canonical_registry"))
    dataset = loaded.datasets["dataset-canonical"]
    split = loaded.splits["split-canonical"]
    prediction = loaded.predictions["prediction-canonical"]
    assert split.dataset_manifest_hash == dataset.hash
    assert prediction.split_manifest_hash == split.hash
    assert prediction.coverage == 0.9


def test_same_id_is_idempotent_but_content_drift_is_refused():
    registry = ManifestRegistry()
    original = registry.add_dataset(_dataset())
    assert registry.add_dataset(_dataset()) is original

    changed = DatasetManifest.create(
        "matches", [DatasetFile("a.mp4", "different", 10)],
        original.schema, original.provenance,
    )
    with pytest.raises(ManifestValidationError, match="hash drift"):
        registry.add_dataset(changed)


def test_manual_persistence_drift_is_detected_on_load(tmp_path):
    from knowledge.ml_registry.storage.registry import Registry
    from knowledge.ml_registry.tests.artifact_projection_fixture import render_legacy_artifact_views

    render_legacy_artifact_views(tmp_path)
    registry = ManifestRegistry.from_registry(Registry(tmp_path / "canonical_registry"))
    with pytest.raises(ManifestValidationError, match="read-only"):
        registry.add_dataset(_dataset())


def test_cross_manifest_references_must_exist():
    registry = ManifestRegistry()
    with pytest.raises(ManifestValidationError, match="unknown dataset"):
        registry.add_split(_split("unknown"))
    with pytest.raises(ManifestValidationError, match="unknown split"):
        registry.add_prediction(_prediction("unknown"))


def test_prediction_group_coverage_must_exactly_match_split_groups():
    registry = ManifestRegistry()
    dataset = registry.add_dataset(_dataset())
    split = registry.add_split(_split(dataset.hash))

    with pytest.raises(ManifestValidationError, match=r"missing=\['match-b'\]"):
        registry.add_prediction(
            _prediction(split.hash, group_coverage={"match-a": 0.9},
                        fold_id_by_group={"match-a": "fold-a"})
        )
    with pytest.raises(ManifestValidationError, match=r"extra=\['invented'\]"):
        registry.add_prediction(
            _prediction(
                split.hash,
                group_coverage={"match-a": 0.9, "match-b": 0.9, "invented": 0.9},
                fold_id_by_group={"match-a": "fold-a", "match-b": "fold-b",
                                  "invented": "fold-a"},
            )
        )
