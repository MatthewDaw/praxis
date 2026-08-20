"""JSON manifest CLI integration."""

from __future__ import annotations

import json

import pytest

from knowledge.ml_registry.manifests import ManifestRegistry
from knowledge.ml_registry.manifests_cli import (
    EXIT_MALFORMED_INPUT,
    EXIT_VALIDATION_ERROR,
    main,
)
from knowledge.ml_registry.portfolio import ArtifactDependency


def _write(path, document):
    path.write_text(json.dumps(document))
    return str(path)


def _invoke(capsys, *args):
    code = main(list(args))
    return code, json.loads(capsys.readouterr().out)


def test_cli_builds_manifest_chain_with_portfolio_ready_outputs(tmp_path, capsys):
    registry_path = tmp_path / "manifests.json"
    dataset_spec = _write(tmp_path / "dataset.json", {
        "manifest_id": "soccer-data",
        "files": [{"identity": "match.mp4", "checksum": "sha-video", "size_bytes": 123}],
        "schema": {"video": "h264"},
        "provenance": {"source": "archive"},
    })
    code, dataset = _invoke(
        capsys, "--file", str(registry_path), "add-dataset", "--spec", dataset_spec
    )
    assert code == 0
    dataset_hash = dataset["manifest"]["hash"]

    split_spec = _write(tmp_path / "split.json", {
        "manifest_id": "soccer-split",
        "dataset_manifest_hash": dataset_hash,
        "assignments": [
            {"group_id": "match-a", "split": "train", "temporal_order": 1},
            {"group_id": "match-b", "split": "validation", "temporal_order": 2},
        ],
    })
    code, split = _invoke(
        capsys, "--file", str(registry_path), "add-split", "--spec", split_spec
    )
    assert code == 0
    split_hash = split["manifest"]["hash"]

    prediction_spec = _write(tmp_path / "prediction.json", {
        "manifest_id": "tracking-predictions",
        "upstream_artifact_id": "tracking-fit-v1",
        "split_manifest_hash": split_hash,
        "predicted_count": 100,
        "eligible_count": 100,
        "coverage": 1.0,
        "schema": {"track_id": "string"},
        "group_coverage": {"match-a": 1.0, "match-b": 1.0},
        "out_of_fold": True,
        "fold_id_by_group": {"match-a": "fold-a", "match-b": "fold-b"},
        "training_groups_by_fold": {"fold-a": ["match-b"], "fold-b": ["match-a"]},
    })
    code, prediction = _invoke(
        capsys, "--file", str(registry_path), "add-prediction", "--spec", prediction_spec
    )
    assert code == 0

    dependency = ArtifactDependency(
        upstream_model_id="tracking",
        artifact_id=prediction["manifest"]["upstream_artifact_id"],
        required_verdict="adopted",
        dataset_manifest_hash=dataset_hash,
        split_manifest_hash=split_hash,
        prediction_manifest_hash=prediction["manifest"]["hash"],
        minimum_coverage=prediction["manifest"]["coverage"],
    )
    assert dependency.prediction_manifest_hash == prediction["manifest"]["hash"]
    assert ManifestRegistry.load(registry_path).predictions["tracking-predictions"].out_of_fold


def test_cli_refuses_hash_drift_without_mutating_registry(tmp_path, capsys):
    registry_path = tmp_path / "manifests.json"
    spec_path = tmp_path / "dataset.json"
    base = {
        "manifest_id": "data",
        "files": [{"identity": "a", "checksum": "sha-a", "size_bytes": 1}],
        "schema": {"x": "int"},
        "provenance": {"source": "archive"},
    }
    _invoke(
        capsys, "--file", str(registry_path), "add-dataset",
        "--spec", _write(spec_path, base),
    )
    before = registry_path.read_bytes()
    base["schema"] = {"x": "float"}

    code, result = _invoke(
        capsys, "--file", str(registry_path), "add-dataset",
        "--spec", _write(spec_path, base),
    )

    assert code == EXIT_VALIDATION_ERROR
    assert result["error"] == "validation"
    assert registry_path.read_bytes() == before


def test_cli_show_and_validate_are_json(tmp_path, capsys):
    path = tmp_path / "manifests.json"
    assert _invoke(capsys, "--file", str(path), "init")[0] == 0
    code, validation = _invoke(capsys, "--file", str(path), "validate")
    assert code == 0
    assert validation == {
        "ok": True, "dataset_count": 0, "split_count": 0, "prediction_count": 0
    }
    code, shown = _invoke(capsys, "--file", str(path), "show")
    assert code == 0
    assert shown["registry"]["schema_version"] == 2


def _seeded_registry(tmp_path, capsys):
    """Build a valid persisted registry through the CLI and return its path."""
    registry_path = tmp_path / "manifests.json"
    dataset_spec = _write(tmp_path / "dataset.json", {
        "manifest_id": "d", "files": [{"identity": "a", "checksum": "sha", "size_bytes": 1}],
        "schema": {"x": "int"}, "provenance": {"source": "test"},
    })
    _invoke(capsys, "--file", str(registry_path), "add-dataset", "--spec", dataset_spec)
    registry = ManifestRegistry.load(registry_path)
    dataset_hash = next(iter(registry.datasets.values())).hash
    split_spec = _write(tmp_path / "split.json", {
        "manifest_id": "s", "dataset_manifest_hash": dataset_hash,
        "assignments": [
            {"group_id": "g1", "split": "train", "temporal_order": 1},
            {"group_id": "g2", "split": "test", "temporal_order": 2},
        ],
    })
    _invoke(capsys, "--file", str(registry_path), "add-split", "--spec", split_spec)
    registry = ManifestRegistry.load(registry_path)
    split_hash = next(iter(registry.splits.values())).hash
    prediction_spec = _write(tmp_path / "prediction.json", {
        "manifest_id": "p", "upstream_artifact_id": "fit-1",
        "split_manifest_hash": split_hash,
        "predicted_count": 1, "eligible_count": 1, "coverage": 1.0,
        "schema": {"y": "float32"},
        "group_coverage": {"g1": 1.0, "g2": 1.0},
        "out_of_fold": True,
        "fold_id_by_group": {"g1": "f1", "g2": "f2"},
        "training_groups_by_fold": {"f1": ["g2"], "f2": ["g1"]},
    })
    code, _ = _invoke(capsys, "--file", str(registry_path), "add-prediction",
                      "--spec", prediction_spec)
    assert code == 0
    return registry_path


@pytest.mark.parametrize("mutation", [
    {"predicted_count": "5"},
    {"fold_id_by_group": 5},
    {"training_groups_by_fold": 5},
])
def test_malformed_persisted_prediction_refuses_without_a_traceback(tmp_path, capsys, mutation):
    registry_path = _seeded_registry(tmp_path, capsys)
    document = json.loads(registry_path.read_text())
    document["predictions"][0].update(mutation)
    registry_path.write_text(json.dumps(document))

    code, output = _invoke(capsys, "--file", str(registry_path), "validate")

    assert code == EXIT_VALIDATION_ERROR
    assert output["ok"] is False


def test_pre_v2_manifest_document_refuses_with_a_migration_message(tmp_path, capsys):
    registry_path = _seeded_registry(tmp_path, capsys)
    document = json.loads(registry_path.read_text())
    document["schema_version"] = 1
    registry_path.write_text(json.dumps(document))

    code, output = _invoke(capsys, "--file", str(registry_path), "validate")

    assert code == EXIT_VALIDATION_ERROR
    assert "predates version 2" in output["message"]


def test_a_pre_v2_split_assignment_without_temporal_order_refuses_cleanly(tmp_path, capsys):
    registry_path = tmp_path / "manifests.json"
    dataset_spec = _write(tmp_path / "dataset.json", {
        "manifest_id": "d", "files": [{"identity": "a", "checksum": "sha", "size_bytes": 1}],
        "schema": {"x": "int"}, "provenance": {"source": "test"},
    })
    _invoke(capsys, "--file", str(registry_path), "add-dataset", "--spec", dataset_spec)
    dataset_hash = next(iter(ManifestRegistry.load(registry_path).datasets.values())).hash
    legacy_spec = _write(tmp_path / "legacy-split.json", {
        "manifest_id": "s", "dataset_manifest_hash": dataset_hash,
        "assignments": [{"group_id": "g1", "split": "train"},
                        {"group_id": "g2", "split": "test"}],
    })

    code, output = _invoke(capsys, "--file", str(registry_path), "add-split", "--spec", legacy_spec)

    assert code in {EXIT_MALFORMED_INPUT, EXIT_VALIDATION_ERROR}
    assert output["ok"] is False
    assert "temporal_order" in output["message"]
