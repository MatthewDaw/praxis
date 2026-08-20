"""Immutable manifest hashing, leakage guards, and persistence."""

from __future__ import annotations

import json

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
    path = tmp_path / "manifests.json"
    registry = ManifestRegistry(path)
    dataset = registry.add_dataset(_dataset())
    split = registry.add_split(_split(dataset.hash))
    prediction = registry.add_prediction(_prediction(split.hash))
    registry.save()

    loaded = ManifestRegistry.load(path)

    assert loaded.datasets[dataset.id].hash == dataset.hash
    assert loaded.splits[split.id].dataset_manifest_hash == dataset.hash
    assert loaded.predictions[prediction.id].split_manifest_hash == split.hash
    assert loaded.predictions[prediction.id].coverage == 0.9


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
    path = tmp_path / "manifests.json"
    registry = ManifestRegistry(path)
    registry.add_dataset(_dataset())
    registry.save()
    document = json.loads(path.read_text())
    document["datasets"][0]["schema"]["frame"] = "tampered"
    path.write_text(json.dumps(document))

    with pytest.raises(ManifestValidationError, match="hash drift"):
        ManifestRegistry.load(path)


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


# --------------------------------------------------------------------------- P15 splits


def test_unknown_split_names_are_refused():
    with pytest.raises(ManifestValidationError, match="unknown split 'holdout'"):
        GroupAssignment("g", "holdout", 1)
    with pytest.raises(ManifestValidationError, match="unknown split 'Train'"):
        GroupAssignment("g", "Train", 1)


@pytest.mark.parametrize("assignments, message", [
    ([("a", "train", 5), ("b", "test", 1)], "train must precede"),
    ([("a", "validation", 3), ("b", "test", 2)], "validation must precede test"),
    ([("a", "train", 5), ("b", "calibration", 0)], "train must precede calibration"),
    ([("a", "calibration", 4), ("b", "validation", 4)], "calibration must precede validation"),
])
def test_every_adjacent_split_pair_is_temporally_ordered(assignments, message):
    with pytest.raises(ManifestValidationError, match=message):
        SplitManifest.create(
            "bad-split", "data-hash",
            [GroupAssignment(*item) for item in assignments],
        )


def test_a_correctly_ordered_four_split_manifest_is_accepted():
    manifest = SplitManifest.create(
        "ordered", "data-hash",
        [
            GroupAssignment("a", "train", 1),
            GroupAssignment("b", "calibration", 2),
            GroupAssignment("c", "validation", 3),
            GroupAssignment("d", "test", 4),
        ],
    )
    assert len(manifest.assignments) == 4


# --------------------------------------------------------------------------- P14 oof proof


@pytest.mark.parametrize("overrides, message", [
    ({"training_groups_by_fold": {"fold-a": [], "fold-b": ["match-a"]}}, "non-empty training"),
    ({"training_groups_by_fold": {"fold-a": ["match-b"], "fold-b": [None]}}, "must all be non-empty strings"),
    ({"training_groups_by_fold": {"fold-a": ["match-b"], "fold-b": [5]}}, "must all be non-empty strings"),
    (
        {
            "fold_id_by_group": {"match-a": "fold-a", "match-b": "fold-a"},
            "training_groups_by_fold": {"fold-a": ["match-z"]},
        },
        "at least two folds",
    ),
])
def test_out_of_fold_proof_shape_is_enforced(overrides, message):
    with pytest.raises(ManifestValidationError, match=message):
        _prediction("any-split-hash", **overrides)


def test_training_groups_outside_the_split_are_refused():
    registry = ManifestRegistry()
    dataset = registry.add_dataset(_dataset())
    split = registry.add_split(_split(dataset.hash))

    with pytest.raises(ManifestValidationError, match=r"absent from the split: \['zzz'\]"):
        registry.add_prediction(
            _prediction(
                split.hash,
                training_groups_by_fold={"fold-a": ["zzz"], "fold-b": ["match-a"]},
            )
        )


def test_an_unreferenced_fold_is_refused():
    registry = ManifestRegistry()
    dataset = registry.add_dataset(_dataset())
    split = registry.add_split(_split(dataset.hash))

    with pytest.raises(ManifestValidationError, match=r"no group is predicted by: \['fold-c'\]"):
        registry.add_prediction(
            _prediction(
                split.hash,
                training_groups_by_fold={
                    "fold-a": ["match-b"], "fold-b": ["match-a"], "fold-c": ["match-a"],
                },
            )
        )


def test_a_fold_may_not_train_on_a_group_later_than_the_evaluation_group_it_predicts():
    registry = ManifestRegistry()
    dataset = registry.add_dataset(_dataset())
    split = SplitManifest.create(
        "match-split", dataset.hash,
        [
            GroupAssignment("match-a", "train", 1),
            GroupAssignment("match-b", "test", 3),
            GroupAssignment("match-d", "test", 4),
        ],
    )
    registry.add_split(split)

    with pytest.raises(ManifestValidationError, match="temporal leakage: fold 'fold-b'"):
        registry.add_prediction(
            PredictionManifest.create(
                "leaky-oof", "fit-v1", split.hash,
                predicted_count=90, eligible_count=100, coverage=0.9,
                schema={"p": "float32"},
                group_coverage={"match-a": 0.9, "match-b": 0.9, "match-d": 0.9},
                out_of_fold=True,
                fold_id_by_group={"match-a": "fold-a", "match-b": "fold-b", "match-d": "fold-c"},
                training_groups_by_fold={
                    "fold-a": ["match-b"], "fold-b": ["match-d"], "fold-c": ["match-a"],
                },
            )
        )


# --------------------------------------------------------------------------- P21 hashing


def test_integral_floats_and_ints_hash_identically():
    first = DatasetManifest.create(
        "ints", [DatasetFile("a", "sha", 1)], {"v": 1}, {"source": "x"}
    )
    second = DatasetManifest.create(
        "ints", [DatasetFile("a", "sha", 1)], {"v": 1.0}, {"source": "x"}
    )
    assert first.hash == second.hash

    registry = ManifestRegistry()
    registry.add_dataset(first)
    assert registry.add_dataset(second) is first


def test_non_finite_schema_values_are_refused():
    with pytest.raises(ManifestValidationError, match="finite"):
        DatasetManifest.create(
            "nan", [DatasetFile("a", "sha", 1)], {"a": float("nan")}, {"source": "x"}
        )


@pytest.mark.parametrize("size", [True, 1.5])
def test_dataset_file_size_must_be_an_integer(size):
    with pytest.raises(ManifestValidationError, match="size_bytes must be an integer"):
        DatasetFile("a", "sha", size)


@pytest.mark.parametrize("field", ["predicted_count", "eligible_count"])
@pytest.mark.parametrize("value", [True, 1.5])
def test_prediction_counts_must_be_integers(field, value):
    with pytest.raises(ManifestValidationError, match="must be an integer"):
        _prediction("any-split-hash", **{field: value})


# --------------------------------------------------------------------------- P13/P16 state


def _registry(tmp_path):
    registry = ManifestRegistry(tmp_path / "manifests.json")
    dataset = registry.add_dataset(_dataset())
    split = registry.add_split(_split(dataset.hash))
    registry.add_prediction(_prediction(split.hash))
    registry.save()
    return registry.path


@pytest.mark.parametrize("mutation", [
    {"predicted_count": "5"},
    {"fold_id_by_group": 5},
    {"training_groups_by_fold": 5},
    {"coverage": True},
])
def test_malformed_persisted_prediction_is_refused_not_raised(tmp_path, mutation):
    path = _registry(tmp_path)
    document = json.loads(path.read_text())
    document["predictions"][0].update(mutation)
    path.write_text(json.dumps(document))

    with pytest.raises(ManifestValidationError):
        ManifestRegistry.load(path)


def test_pre_v2_manifest_registry_is_refused_with_a_migration_message(tmp_path):
    path = _registry(tmp_path)
    document = json.loads(path.read_text())
    document["schema_version"] = 1
    path.write_text(json.dumps(document))

    with pytest.raises(ManifestValidationError, match="predates version 2"):
        ManifestRegistry.load(path)
