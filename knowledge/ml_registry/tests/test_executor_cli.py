import json
from pathlib import Path
import sys

from knowledge.ml_registry.executor_cli import main


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload))
    return path


def test_cli_dry_run_consumes_scheduler_job_spec(tmp_path: Path, capsys):
    job = _write(tmp_path / "job.json", {
        "campaign_id": "soccer", "command": [sys.executable, "-c", "raise SystemExit(9)"],
        "resources": {"cpus": 1}, "environment": {}, "checkpoint_uri": "artifact://soccer",
    })
    state = tmp_path / "state.json"
    assert main(["run-job", "--job", str(job), "--state", str(state),
                 "--log-dir", str(tmp_path / "logs"), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "dry_run"
    assert json.loads(state.read_text())["campaign_id"] == "soccer"


def test_cli_reports_process_failure(tmp_path: Path, capsys):
    job = _write(tmp_path / "job.json", {
        "campaign_id": "bad", "command": [sys.executable, "-c", "raise SystemExit(4)"],
        "resources": {"cpus": 1},
    })
    assert main(["run-job", "--job", str(job), "--state", str(tmp_path / "state.json"),
                 "--log-dir", str(tmp_path / "logs")]) == 1
    assert json.loads(capsys.readouterr().out)["returncode"] == 4


def test_cli_malformed_job_is_exit_two(tmp_path: Path, capsys):
    job = tmp_path / "job.json"
    job.write_text("[]")
    assert main(["run-job", "--job", str(job), "--state", str(tmp_path / "state.json"),
                 "--log-dir", str(tmp_path / "logs")]) == 2
    assert "MALFORMED INPUT" in capsys.readouterr().err
