from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from knowledge.ml_registry.scheduler_cli import main


def _write(tmp_path: Path, name: str, payload: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


def _inputs(tmp_path: Path):
    campaigns = _write(tmp_path, "campaigns.json", [
        {"id": "value", "depends_on": ["tracking"], "command": ["train", "value"]},
        {"id": "tracking", "priority": 1, "command": ["train", "tracking"]},
    ])
    states = _write(tmp_path, "states.json", {})
    capacity = _write(tmp_path, "capacity.json", {
        "resources": {"cpus": 2, "ram_gb": 4}, "max_concurrency": 1, "remaining_cost": 10,
    })
    return campaigns, states, capacity


def test_schedule_portfolio_emits_stable_read_only_json(tmp_path: Path, capsys):
    campaigns, states, capacity = _inputs(tmp_path)
    before = {path: path.read_text() for path in (campaigns, states, capacity)}
    assert main(["schedule-portfolio", "--campaigns", str(campaigns),
                 "--states", str(states), "--capacity", str(capacity)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert [job["campaign_id"] for job in output["jobs"]] == ["tracking"]
    assert output["blocked"] == {"value": "waiting for dependencies: tracking"}
    assert output["available"]["cpus"] == 1
    assert before == {path: path.read_text() for path in (campaigns, states, capacity)}


def test_state_list_and_top_level_capacity_are_accepted(tmp_path: Path, capsys):
    campaigns = _write(tmp_path, "c.json", [
        {"id": "base", "command": ["base"]},
        {"id": "child", "depends_on": ["base"], "command": ["child"]},
    ])
    states = _write(tmp_path, "s.json", [{"campaign_id": "base", "state": "completed"}])
    capacity = _write(tmp_path, "r.json", {"cpus": 1, "ram_gb": 2, "max_concurrency": 1})
    assert main(["schedule-portfolio", "--campaigns", str(campaigns),
                 "--states", str(states), "--capacity", str(capacity)]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"][0]["campaign_id"] == "child"


def test_invalid_graph_is_a_refusal(tmp_path: Path, capsys):
    campaigns = _write(tmp_path, "c.json", [{"id": "a", "depends_on": ["missing"], "command": ["a"]}])
    states = _write(tmp_path, "s.json", {})
    capacity = _write(tmp_path, "r.json", {"cpus": 1, "max_concurrency": 1})
    assert main(["schedule-portfolio", "--campaigns", str(campaigns),
                 "--states", str(states), "--capacity", str(capacity)]) == 1
    assert "REFUSED: unknown dependencies: missing" in capsys.readouterr().err


def test_malformed_json_has_exit_two(tmp_path: Path, capsys):
    campaigns = tmp_path / "bad.json"
    campaigns.write_text("{")
    states = _write(tmp_path, "s.json", {})
    capacity = _write(tmp_path, "r.json", {"cpus": 1, "max_concurrency": 1})
    assert main(["schedule-portfolio", "--campaigns", str(campaigns),
                 "--states", str(states), "--capacity", str(capacity)]) == 2
    assert "MALFORMED INPUT:" in capsys.readouterr().err


def test_python_module_entrypoint(tmp_path: Path):
    campaigns, states, capacity = _inputs(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "knowledge.ml_registry.scheduler_cli", "schedule-portfolio",
         "--campaigns", str(campaigns), "--states", str(states), "--capacity", str(capacity)],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["jobs"][0]["campaign_id"] == "tracking"
