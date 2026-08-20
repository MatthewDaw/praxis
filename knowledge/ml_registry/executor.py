"""Execution seam for scheduler jobs, with a safe local subprocess adapter."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Callable, Mapping, Protocol

from knowledge.ml_registry.scheduler import JobSpec


class ExecutorError(ValueError):
    """An execution request is unsafe or malformed."""


@dataclass(frozen=True)
class ExecutionResult:
    campaign_id: str
    state: str
    returncode: int | None
    started_at: float | None
    finished_at: float | None
    stdout_log: str | None
    stderr_log: str | None
    checkpoint_uri: str | None
    resume_from: str | None
    message: str | None = None
    artifact: Mapping[str, object] | None = None


class ExecutionBackend(Protocol):
    def execute(self, job: JobSpec, *, state_path: Path, dry_run: bool = False) -> ExecutionResult:
        """Execute or describe one job, durably recording its resulting state."""


BackendFactory = Callable[..., ExecutionBackend]
_BACKENDS: dict[str, BackendFactory] = {}


def register_backend(name: str, factory: BackendFactory, *, replace: bool = False) -> None:
    """Register an adapter factory without coupling Praxis to its infrastructure."""
    if not name or not name.replace("-", "_").isidentifier():
        raise ExecutorError(f"invalid backend name: {name!r}")
    if name in _BACKENDS and not replace:
        raise ExecutorError(f"backend already registered: {name}")
    _BACKENDS[name] = factory


def create_backend(name: str, **kwargs: object) -> ExecutionBackend:
    try:
        factory = _BACKENDS[name]
    except KeyError as exc:
        raise ExecutorError(f"unknown backend: {name}") from exc
    return factory(**kwargs)


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class LocalSubprocessBackend:
    """Run an argv vector locally without a shell or ambient environment leakage."""

    DEFAULT_ENV_ALLOWLIST = frozenset({"PATH", "LANG", "LC_ALL", "TMPDIR"})

    def __init__(self, *, log_dir: str | Path, env_allowlist: set[str] | frozenset[str] | None = None):
        self.log_dir = Path(log_dir)
        self.env_allowlist = frozenset(
            self.DEFAULT_ENV_ALLOWLIST if env_allowlist is None else env_allowlist
        )

    def _environment(self, overrides: Mapping[str, str]) -> dict[str, str]:
        refused = sorted(set(overrides) - self.env_allowlist)
        if refused:
            raise ExecutorError("environment overrides are not allowlisted: " + ", ".join(refused))
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in overrides.items()):
            raise ExecutorError("environment overrides must be strings")
        environment = {key: value for key, value in os.environ.items() if key in self.env_allowlist}
        environment.update(overrides)
        return environment

    @staticmethod
    def _artifact(job: JobSpec) -> dict[str, object] | None:
        if job.artifact_result_path is None:
            return None
        path = Path(job.artifact_result_path)
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ExecutorError(f"artifact result is unavailable or malformed: {exc}") from exc
        if not isinstance(payload, dict):
            raise ExecutorError("artifact result must be a JSON object")
        required_strings = (
            "artifact_id", "model_id", "verdict", "dataset_manifest_hash",
            "split_manifest_hash", "prediction_manifest_hash", "producer_campaign_id",
        )
        missing = [name for name in required_strings
                   if not isinstance(payload.get(name), str) or not payload[name].strip()]
        if missing:
            raise ExecutorError("artifact result has missing/invalid fields: " + ", ".join(missing))
        if payload["producer_campaign_id"] != job.campaign_id:
            raise ExecutorError(
                f"artifact producer_campaign_id {payload['producer_campaign_id']!r} does not match "
                f"job campaign_id {job.campaign_id!r}"
            )
        coverage = payload.get("coverage")
        if (not isinstance(coverage, (int, float)) or isinstance(coverage, bool)
                or not 0 <= coverage <= 1):
            raise ExecutorError("artifact result coverage must be a number between 0 and 1")
        return payload

    def execute(self, job: JobSpec, *, state_path: Path, dry_run: bool = False) -> ExecutionResult:
        if not job.command or not all(isinstance(arg, str) and arg for arg in job.command):
            raise ExecutorError("job command must be a non-empty argv vector of non-empty strings")
        if Path(job.campaign_id).name != job.campaign_id or job.campaign_id in {"", ".", ".."}:
            raise ExecutorError("campaign_id must be a safe single path component")
        environment = self._environment(job.environment)
        timeout = job.timeout_minutes or job.resources.wall_time_minutes
        if timeout <= 0:
            raise ExecutorError("job timeout must be positive")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = self.log_dir / f"{job.campaign_id}.stdout.log"
        stderr_path = self.log_dir / f"{job.campaign_id}.stderr.log"

        if dry_run:
            result = ExecutionResult(
                job.campaign_id, "dry_run", None, None, None, str(stdout_path), str(stderr_path),
                job.checkpoint_uri, job.resume_from, "command validated but not executed",
            )
            _atomic_json(state_path, asdict(result))
            return result

        started = time.time()
        running = ExecutionResult(
            job.campaign_id, "running", None, started, None, str(stdout_path), str(stderr_path),
            job.checkpoint_uri, job.resume_from,
        )
        _atomic_json(state_path, asdict(running))
        try:
            completed = subprocess.run(
                list(job.command),
                shell=False,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout * 60,
                check=False,
            )
            stdout_path.write_bytes(completed.stdout)
            stderr_path.write_bytes(completed.stderr)
            final_state = "completed" if completed.returncode == 0 else "failed"
            artifact = None
            message = None
            if final_state == "completed":
                try:
                    artifact = self._artifact(job)
                except ExecutorError as exc:
                    final_state = "failed"
                    message = str(exc)
            result = ExecutionResult(
                job.campaign_id, final_state, completed.returncode, started, time.time(),
                str(stdout_path), str(stderr_path), job.checkpoint_uri, job.resume_from,
                message, artifact,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_path.write_bytes(exc.stdout or b"")
            stderr_path.write_bytes(exc.stderr or b"")
            result = ExecutionResult(
                job.campaign_id, "timed_out", None, started, time.time(), str(stdout_path),
                str(stderr_path), job.checkpoint_uri, job.resume_from,
                f"exceeded timeout of {timeout} minutes",
            )
        except OSError as exc:
            stdout_path.write_bytes(b"")
            stderr_path.write_text(str(exc))
            result = ExecutionResult(
                job.campaign_id, "failed", None, started, time.time(), str(stdout_path),
                str(stderr_path), job.checkpoint_uri, job.resume_from, str(exc),
            )
        _atomic_json(state_path, asdict(result))
        return result


register_backend("local", LocalSubprocessBackend)
