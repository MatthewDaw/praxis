from __future__ import annotations

import json

from knowledge.ml_registry.portfolio_cli import EXIT_VALIDATION_ERROR, main


def test_path_portfolio_cli_refuses_without_creating_state(tmp_path, capsys) -> None:
    path = tmp_path / "portfolio.json"
    assert main(["--file", str(path), "init"]) == EXIT_VALIDATION_ERROR
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "validation"
    assert "canonical registry" in result["message"]
    assert not path.exists()


def test_path_portfolio_cli_refuses_mutation(tmp_path, capsys) -> None:
    path = tmp_path / "portfolio.json"
    assert main([
        "--file", str(path), "add-artifact", "--artifact-id", "old",
        "--model-id", "old", "--verdict", "adopted", "--dataset-hash", "d",
        "--split-hash", "s", "--prediction-hash", "p", "--coverage", "1",
    ]) == EXIT_VALIDATION_ERROR
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert not path.exists()
