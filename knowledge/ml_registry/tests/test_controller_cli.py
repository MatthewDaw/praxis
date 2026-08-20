import json
from pathlib import Path

from knowledge.ml_registry.controller_cli import main
from knowledge.ml_registry.portfolio import CampaignStatus, Portfolio


def write(path: Path, payload):
    path.write_text(json.dumps(payload))
    return path


def test_one_shot_controller_cli_dispatches_at_most_two(tmp_path: Path, capsys):
    portfolio_path = tmp_path / "portfolio.json"
    portfolio = Portfolio(portfolio_path)
    for cid in ("a", "b", "c"):
        portfolio.add_campaign(cid, cid).status = CampaignStatus.READY
    portfolio.save()
    campaigns = write(tmp_path / "campaigns.json", [
        {"id": cid, "command": ["true"], "resources": {"cpus": 1}} for cid in ("a", "b", "c")
    ])
    capacity = write(tmp_path / "capacity.json", {
        "resources": {"cpus": 4, "ram_gb": 8}, "max_concurrency": 2,
    })
    code = main(["run-portfolio", "--portfolio", str(portfolio_path),
                 "--campaigns", str(campaigns), "--capacity", str(capacity),
                 "--controller-state", str(tmp_path / "controller.json"),
                 "--dispatch-dir", str(tmp_path / "dispatch"), "--one-shot"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert len(output["started"]) == 2
    assert len(json.loads((tmp_path / "controller.json").read_text())["records"]) == 2


def test_controller_cli_reports_blocked(tmp_path: Path, capsys):
    portfolio_path = tmp_path / "portfolio.json"
    portfolio = Portfolio(portfolio_path)
    portfolio.add_campaign("blocked", "blocked").status = CampaignStatus.BLOCKED
    portfolio.save()
    campaigns = write(tmp_path / "campaigns.json", [{"id": "blocked", "command": ["true"]}])
    capacity = write(tmp_path / "capacity.json", {"cpus": 1, "max_concurrency": 2})
    assert main(["run-portfolio", "--portfolio", str(portfolio_path),
                 "--campaigns", str(campaigns), "--capacity", str(capacity),
                 "--controller-state", str(tmp_path / "state.json"),
                 "--dispatch-dir", str(tmp_path / "dispatch"), "--one-shot"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
