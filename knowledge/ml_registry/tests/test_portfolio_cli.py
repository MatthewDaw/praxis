from __future__ import annotations

import pytest

from knowledge.ml_registry.cli.portfolio import main as canonical_main
from knowledge.ml_registry.portfolio_cli import main


def test_compatibility_facade_is_the_canonical_operator(tmp_path, capsys) -> None:
    assert main is canonical_main
    path = tmp_path / "portfolio.json"
    with pytest.raises(SystemExit, match="2"):
        main(["--file", str(path), "init"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid choice" in captured.err
    assert not path.exists()


def test_path_portfolio_cli_refuses_mutation(tmp_path, capsys) -> None:
    path = tmp_path / "portfolio.json"
    with pytest.raises(SystemExit, match="2"):
        main([
            "--file", str(path), "add-artifact", "--artifact-id", "old",
            "--model-id", "old", "--verdict", "adopted", "--dataset-hash", "d",
            "--split-hash", "s", "--prediction-hash", "p", "--coverage", "1",
        ])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid choice" in captured.err
    assert not path.exists()
