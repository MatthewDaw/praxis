from __future__ import annotations

import json
from pathlib import Path
import sys

from knowledge.ml_registry import Registry
from knowledge.ml_registry.cli.portfolio import main


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value))
    return path


def _config(tmp_path: Path) -> Path:
    Registry(tmp_path / "registry").register_campaign_spec({
        "schema_version": 1, "campaign_id": "R1", "model_id_policy": "model-R1",
        "axis": "fixture", "sport_scope": "shared", "target_ontology": "fixture",
        "metric": {"name": "f1"}, "stages": [{"name": "representation"}],
        "corpora": [{"id": "fixture"}], "requires": [],
        "produces": [{"artifact_type": "fit", "schema_version": "1", "oof_for": []}],
        "supervision": {"mode": "composing"}, "resources": {"lane": "cpu"},
        "isolation": {"state_root": "state/R1"},
        "production": {"protocol": "Fixture"}, "extends": [],
        "deterministic_incumbent": None, "learned_escalation": False,
    })
    _write(tmp_path / "portfolio.json", {
        "schema_version": 1,
        "campaigns": [{
            "id": "R1", "model_id": "model-R1", "dependencies": [], "status": "READY",
            "stale": False, "blocked_reasons": [], "history": [],
        }],
        "artifacts": [],
    })
    _write(tmp_path / "campaigns.json", [{
        "id": "R1", "command": [sys.executable, "-c", "import time; time.sleep(30)"],
        "resources": {"cpus": 1}, "timeout_minutes": 1,
        "lease": {"lane": "cpu", "device": "cpu:R1", "cpu_threads": 1},
    }])
    _write(tmp_path / "capacity.json", {
        "resources": {"cpus": 2, "ram_gb": 2}, "max_concurrency": 2,
    })
    _write(tmp_path / "space.json", {"schema_version": 1, "facts": []})
    return _write(tmp_path / "operator.json", {
        "schema_version": 1,
        "portfolio": "portfolio.json",
        "campaigns": "campaigns.json",
        "capacity": "capacity.json",
        "registry_root": "registry",
        "space_file": "space.json",
        "runtime_root": "runtime",
        "max_active": 2,
        "finalization": {"R1": {
            "experiment_id": "R1", "model_id": "model-R1", "model_fact_id": "fact-R1",
            "version": 1, "artifact_type": "fit", "compatibility_adapter": "builtins:bool",
        }},
    })


def test_operator_run_status_explain_and_force_stop_share_durable_runtime(tmp_path: Path, capsys) -> None:
    config = _config(tmp_path)
    assert main(["--config", str(config), "run", "--one-shot"]) == 0
    started = json.loads(capsys.readouterr().out)
    assert started["started"] == ["R1"]

    assert main(["--config", str(config), "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    occupied = next(slot for slot in status["slots"] if slot["campaign_id"] == "R1")
    assert occupied["lease"]["device"] == "cpu:R1"
    assert "ready_frontier" in status

    assert main(["--config", str(config), "explain", "R1"]) == 0
    explained = json.loads(capsys.readouterr().out)
    assert explained["registered_model"] == "model-R1"
    assert explained["lease"]["cpu_threads"] == 1

    assert main(["--config", str(config), "stop", "--force"]) == 0
    stopped = json.loads(capsys.readouterr().out)
    assert stopped["forced_terminations"] == ["R1"]
    assert json.loads((tmp_path / "runtime" / "ownership.json").read_text())["leases"] == []


def test_operator_refuses_campaign_without_finalization_binding(tmp_path: Path, capsys) -> None:
    config = _config(tmp_path)
    payload = json.loads(config.read_text())
    payload["finalization"] = {}
    config.write_text(json.dumps(payload))
    assert main(["--config", str(config), "status"]) == 2
    assert "every campaign requires a finalization binding" in capsys.readouterr().err
