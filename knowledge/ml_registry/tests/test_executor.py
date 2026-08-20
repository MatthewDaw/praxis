from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest
import subprocess

from knowledge.ml_registry.executor import ExecutorError, LocalSubprocessBackend, create_backend, register_backend
from knowledge.ml_registry.scheduler import JobSpec, ResourceProfile


def job(*command: str, **kwargs) -> JobSpec:
    return JobSpec("campaign", command, ResourceProfile(), **kwargs)


def test_local_backend_executes_argv_without_shell_and_captures_logs(tmp_path: Path):
    marker = tmp_path / "must-not-exist"
    backend = LocalSubprocessBackend(log_dir=tmp_path / "logs")
    spec = job(sys.executable, "-c", "import sys; print(sys.argv[1]); print('err', file=sys.stderr)",
               f"literal;touch {marker}")
    result = backend.execute(spec, state_path=tmp_path / "state.json")
    assert result.state == "completed"
    assert f"literal;touch {marker}" in Path(result.stdout_log).read_text()
    assert Path(result.stderr_log).read_text().strip() == "err"
    assert not marker.exists()
    assert json.loads((tmp_path / "state.json").read_text())["state"] == "completed"


def test_environment_is_filtered_and_overrides_require_allowlisting(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SECRET_VALUE", "hidden")
    backend = LocalSubprocessBackend(log_dir=tmp_path, env_allowlist={"SAFE"})
    result = backend.execute(
        job(sys.executable, "-c", "import os; print(os.getenv('SAFE')); print(os.getenv('SECRET_VALUE'))",
            environment={"SAFE": "visible"}), state_path=tmp_path / "state.json",
    )
    assert Path(result.stdout_log).read_text().splitlines() == ["visible", "None"]
    with pytest.raises(ExecutorError, match="not allowlisted: SECRET_VALUE"):
        backend.execute(job("true", environment={"SECRET_VALUE": "x"}), state_path=tmp_path / "no.json")


def test_dry_run_writes_state_without_execution(tmp_path: Path):
    marker = tmp_path / "marker"
    backend = LocalSubprocessBackend(log_dir=tmp_path / "logs")
    result = backend.execute(job(sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"),
                             state_path=tmp_path / "state.json", dry_run=True)
    assert result.state == "dry_run"
    assert not marker.exists()
    assert json.loads((tmp_path / "state.json").read_text())["state"] == "dry_run"


def test_failure_and_checkpoint_metadata_are_durable(tmp_path: Path):
    backend = LocalSubprocessBackend(log_dir=tmp_path / "logs")
    result = backend.execute(
        job(sys.executable, "-c", "raise SystemExit(7)", checkpoint_uri="artifact://next",
            resume_from="artifact://prior"), state_path=tmp_path / "state.json",
    )
    state = json.loads((tmp_path / "state.json").read_text())
    assert (result.state, result.returncode) == ("failed", 7)
    assert (state["checkpoint_uri"], state["resume_from"]) == ("artifact://next", "artifact://prior")


def test_declared_artifact_result_is_validated_and_embedded(tmp_path: Path):
    result_path = tmp_path / "artifact.json"
    payload = {
        "artifact_id": "fit-v1", "model_id": "tracking", "verdict": "adopted",
        "dataset_manifest_hash": "data", "split_manifest_hash": "split",
        "prediction_manifest_hash": "pred", "coverage": 1,
        "producer_campaign_id": "campaign",
    }
    command = (sys.executable, "-c", f"import json; open({str(result_path)!r}, 'w').write(json.dumps({payload!r}))")
    result = LocalSubprocessBackend(log_dir=tmp_path / "logs").execute(
        job(*command, artifact_result_path=str(result_path)), state_path=tmp_path / "state.json",
    )
    assert result.state == "completed"
    assert result.artifact == payload
    assert json.loads((tmp_path / "state.json").read_text())["artifact"]["artifact_id"] == "fit-v1"


@pytest.mark.parametrize("payload, match", [
    ({}, "missing/invalid fields"),
    ({"artifact_id": "x", "model_id": "m", "verdict": "adopted",
      "dataset_manifest_hash": "d", "split_manifest_hash": "s",
      "prediction_manifest_hash": "p", "coverage": 1,
      "producer_campaign_id": "some-other-campaign"}, "does not match"),
])
def test_completed_process_with_invalid_required_artifact_fails(tmp_path: Path, payload, match):
    result_path = tmp_path / "artifact.json"
    command = (sys.executable, "-c",
               f"import json; open({str(result_path)!r}, 'w').write(json.dumps({payload!r}))")
    result = LocalSubprocessBackend(log_dir=tmp_path / "logs").execute(
        job(*command, artifact_result_path=str(result_path)),
        state_path=tmp_path / "state.json",
    )
    assert result.state == "failed"
    assert match in result.message


def test_backend_registration_is_pluggable():
    class Stub:
        pass
    register_backend("test-adapter", lambda **kwargs: Stub(), replace=True)
    assert isinstance(create_backend("test-adapter"), Stub)
    with pytest.raises(ExecutorError, match="unknown backend"):
        create_backend("does-not-exist")


def test_timeout_is_recorded_atomically(tmp_path: Path, monkeypatch):
    class NeverFinishes:
        pid = 424242
        returncode = None
        def poll(self): return None
        def wait(self, timeout=None):
            self.returncode = -15
            return self.returncode
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: NeverFinishes())
    monkeypatch.setattr("knowledge.ml_registry.executor.time.time", iter([0.0, 61.0, 61.0, 61.0]).__next__)
    monkeypatch.setattr("knowledge.ml_registry.executor.os.killpg", lambda *args: None)
    result = LocalSubprocessBackend(log_dir=tmp_path / "logs").execute(
        job("worker", timeout_minutes=1), state_path=tmp_path / "state.json",
    )
    assert result.state == "timed_out"
    assert Path(result.stdout_log).read_bytes() == b""
    assert json.loads((tmp_path / "state.json").read_text())["state"] == "timed_out"


def test_campaign_id_cannot_escape_log_directory(tmp_path: Path):
    unsafe = JobSpec("../escape", ("true",), ResourceProfile())
    with pytest.raises(ExecutorError, match="safe single path component"):
        LocalSubprocessBackend(log_dir=tmp_path).execute(unsafe, state_path=tmp_path / "state.json")


def test_working_directory_and_heartbeat_are_recorded(tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir()
    result = LocalSubprocessBackend(log_dir=tmp_path / "logs").execute(
        job(sys.executable, "-c", "import os; print(os.getcwd())",
            working_directory=str(work)), state_path=tmp_path / "state.json",
    )
    state = json.loads((tmp_path / "state.json").read_text())
    assert Path(result.stdout_log).read_text().strip() == str(work)
    assert state["pid"] > 0
    assert state["heartbeat_at"] >= state["started_at"]
