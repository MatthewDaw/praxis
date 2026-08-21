from __future__ import annotations

from knowledge.ml_registry.controller_cli import main


def test_path_portfolio_controller_refuses_before_opening_inputs(tmp_path, capsys) -> None:
    assert main([
        "run-portfolio", "--portfolio", str(tmp_path / "portfolio.json"),
        "--campaigns", str(tmp_path / "campaigns.json"),
        "--capacity", str(tmp_path / "capacity.json"),
        "--controller-state", str(tmp_path / "controller.json"),
        "--dispatch-dir", str(tmp_path / "dispatch"), "--one-shot",
    ]) == 1
    assert "knowledge.ml_registry.cli.portfolio" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []
