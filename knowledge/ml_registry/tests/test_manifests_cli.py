from __future__ import annotations

import json

from knowledge.ml_registry.manifests_cli import EXIT_VALIDATION_ERROR, main


def test_path_manifest_cli_refuses_without_creating_state(tmp_path, capsys) -> None:
    path = tmp_path / "manifests.json"
    assert main(["--file", str(path), "init"]) == EXIT_VALIDATION_ERROR
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "validation"
    assert "canonical registry" in result["message"]
    assert not path.exists()


def test_path_manifest_cli_refuses_mutation_before_reading_spec(tmp_path, capsys) -> None:
    path = tmp_path / "manifests.json"
    missing = tmp_path / "missing-spec.json"
    assert main(["--file", str(path), "add-dataset", "--spec", str(missing)]) == EXIT_VALIDATION_ERROR
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert not path.exists()
