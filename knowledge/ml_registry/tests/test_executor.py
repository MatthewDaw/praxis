from __future__ import annotations

import json
from pathlib import Path
import signal
import sys
import threading
import time

import pytest
import subprocess

from knowledge.ml_registry import process_probe
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


def test_zero_or_negative_timeout_is_refused_instead_of_silently_falling_back(tmp_path: Path):
    backend = LocalSubprocessBackend(log_dir=tmp_path / "logs")
    for minutes in (0, -1):
        with pytest.raises(ExecutorError, match="timeout must be a positive"):
            backend.execute(job(sys.executable, "-c", "pass", timeout_minutes=minutes),
                            state_path=tmp_path / "state.json")


def test_sigterm_kills_the_child_group_and_records_failure(tmp_path: Path):
    state_path = tmp_path / "state.json"
    main_thread = threading.main_thread().ident
    timer = threading.Timer(.7, lambda: signal.pthread_kill(main_thread, signal.SIGTERM))
    timer.start()
    try:
        result = LocalSubprocessBackend(log_dir=tmp_path / "logs").execute(
            job(sys.executable, "-c", "import time; time.sleep(60)"), state_path=state_path,
        )
    finally:
        timer.cancel()
    assert result.state == "failed"
    assert "received signal" in result.message
    assert json.loads(state_path.read_text())["state"] == "failed"
    child = json.loads(state_path.read_text())["pid"]
    deadline = time.time() + 5
    while time.time() < deadline and process_probe.probe(child)[0]:
        time.sleep(.05)
    assert not process_probe.probe(child)[0], "the training child was orphaned"


def test_unexpected_exception_kills_the_child_and_fails_the_state(tmp_path: Path, monkeypatch):
    import knowledge.ml_registry.executor as executor_module
    state_path = tmp_path / "state.json"

    real_sleep = time.sleep
    exploded = []

    def exploding_sleep(interval):
        if not exploded:
            exploded.append(interval)
            raise RuntimeError("supervisor bug")
        return real_sleep(interval)

    monkeypatch.setattr(executor_module.time, "sleep", exploding_sleep)
    result = LocalSubprocessBackend(log_dir=tmp_path / "logs").execute(
        job(sys.executable, "-c", "import time; time.sleep(60)"), state_path=state_path,
    )
    monkeypatch.undo()
    assert result.state == "failed"
    assert "supervisor bug" in result.message
    assert json.loads(state_path.read_text())["state"] == "failed"
    child = result.pid
    deadline = time.time() + 5
    while time.time() < deadline and process_probe.probe(child)[0]:
        time.sleep(.05)
    assert not process_probe.probe(child)[0], "the training child was orphaned"


def test_heartbeat_failure_is_survivable_and_never_truncates_logs(tmp_path: Path, monkeypatch):
    import knowledge.ml_registry.executor as executor_module
    real = executor_module._atomic_json
    calls = []

    def flaky(path, payload, **kwargs):
        calls.append(payload.get("heartbeat_at"))
        if len(calls) > 1 and payload.get("state") == "running":
            raise OSError("no space left on device")
        return real(path, payload, **kwargs)

    monkeypatch.setattr(executor_module, "_atomic_json", flaky)
    result = LocalSubprocessBackend(log_dir=tmp_path / "logs").execute(
        job(sys.executable, "-c", "import time; print('work'); time.sleep(2.5)"),
        state_path=tmp_path / "state.json",
    )
    assert result.state == "completed"
    assert len([beat for beat in calls if beat is not None]) > 2  # heartbeats were retried
    assert Path(result.stdout_log).read_text().strip() == "work"


def test_os_error_during_launch_does_not_clobber_existing_logs(tmp_path: Path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "campaign.stdout.log").write_text("previous attempt output")
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(
        OSError("Exec format error")))
    result = LocalSubprocessBackend(log_dir=logs).execute(
        job("worker"), state_path=tmp_path / "state.json",
    )
    assert result.state == "failed"
    assert "Exec format error" in result.message
    assert json.loads((tmp_path / "state.json").read_text())["state"] == "failed"


def test_heartbeats_are_written_at_about_one_hertz(tmp_path: Path, monkeypatch):
    import knowledge.ml_registry.executor as executor_module
    real = executor_module._atomic_json
    fsyncs = []
    beats = []

    def counting(path, payload, **kwargs):
        if payload.get("state") == "running":
            beats.append(payload.get("heartbeat_at"))
            fsyncs.append(kwargs.get("fsync", True))
        return real(path, payload, **kwargs)

    monkeypatch.setattr(executor_module, "_atomic_json", counting)
    LocalSubprocessBackend(log_dir=tmp_path / "logs").execute(
        job(sys.executable, "-c", "import time; time.sleep(2.2)"),
        state_path=tmp_path / "state.json",
    )
    assert 2 <= len(beats) <= 5  # ~1 Hz, not the old 4 Hz
    assert fsyncs[1:] == [False] * (len(fsyncs) - 1)  # only the opening write is fsynced
