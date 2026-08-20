"""Persistent, dependency-safe controller for ML portfolio execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Protocol, Sequence

from knowledge.ml_registry import process_probe
from knowledge.ml_registry.executor import LocalSubprocessBackend
from knowledge.ml_registry.portfolio import CampaignStatus, Portfolio
from knowledge.ml_registry.scheduler import JobSpec, JobState, ResourceProfile, ScheduleDecision, schedule


MAX_ACTIVE_CAMPAIGNS = 2
LAUNCH_TIMEOUT_SECONDS = 120.0
LIVE_STATES = frozenset({"running", "dispatching"})


class ControllerError(ValueError):
    pass


@dataclass(frozen=True)
class PollResult:
    state: str
    artifact: Mapping[str, Any] | None = None
    message: str | None = None
    checkpoint_uri: str | None = None
    attempt_token: str | None = None


class AsyncBackend(Protocol):
    def submit(self, job: JobSpec) -> str: ...
    def poll(self, backend_job_id: str) -> PollResult: ...


@dataclass
class DispatchRecord:
    backend_job_id: str
    state: str = "running"
    attempt: int = 1
    next_retry_at: float = 0.0
    message: str | None = None
    started_at: float | None = None
    checkpoint_uri: str | None = None


@dataclass(frozen=True)
class TickResult:
    status: str
    started: tuple[str, ...]
    running: tuple[str, ...]
    completed: tuple[str, ...]
    blocked: Mapping[str, str]


def _atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def portfolio_schedule(
    portfolio: Portfolio,
    campaign_specs: Sequence[Mapping[str, Any]],
    states: Mapping[str, JobState | Mapping[str, Any]],
    capacity: ResourceProfile | Mapping[str, Any],
    *,
    max_active: int = MAX_ACTIVE_CAMPAIGNS,
    remaining_cost: float | None = None,
) -> ScheduleDecision:
    """Schedule only READY campaigns whose exact artifact contracts remain current."""
    if not 1 <= max_active <= MAX_ACTIVE_CAMPAIGNS:
        raise ControllerError(f"max_active must be between 1 and {MAX_ACTIVE_CAMPAIGNS}")
    gated: dict[str, JobState | Mapping[str, Any]] = dict(states)
    for spec in campaign_specs:
        campaign_id = str(spec.get("id", ""))
        if campaign_id not in portfolio.campaigns:
            raise ControllerError(f"campaign {campaign_id!r} is missing from the portfolio")
        existing = gated.get(campaign_id)
        existing_state = existing.state if isinstance(existing, JobState) else (
            existing.get("state") if isinstance(existing, Mapping) else None
        )
        if existing_state in {"running", "completed"}:
            continue
        readiness = portfolio.refresh(campaign_id)
        campaign = portfolio.campaigns[campaign_id]
        if campaign.status != CampaignStatus.READY or campaign.stale or not readiness.activatable:
            reasons = list(readiness.reasons)
            if campaign.status != CampaignStatus.READY:
                reasons.insert(0, f"campaign status is {campaign.status.value}, expected READY")
            gated[campaign_id] = JobState(campaign_id, "blocked", message="; ".join(reasons))
    return schedule(
        campaign_specs, gated, capacity, max_concurrency=max_active,
        remaining_cost=remaining_cost,
    )


class PortfolioController:
    def __init__(self, *, portfolio: Portfolio, campaign_specs: Sequence[Mapping[str, Any]],
                 capacity: ResourceProfile | Mapping[str, Any], backend: AsyncBackend,
                 state_path: str | Path, max_active: int = MAX_ACTIVE_CAMPAIGNS,
                 retry_backoff_seconds: float = 60.0, max_retry_backoff_seconds: float = 3600.0,
                 clock=time.time):
        if not 1 <= max_active <= MAX_ACTIVE_CAMPAIGNS:
            raise ControllerError(f"max_active must be between 1 and {MAX_ACTIVE_CAMPAIGNS}")
        self.portfolio = portfolio
        self.specs = list(campaign_specs)
        self.capacity = capacity
        self.backend = backend
        self.state_path = Path(state_path)
        self.max_active = max_active
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_retry_backoff_seconds = max_retry_backoff_seconds
        self.clock = clock
        self.records: dict[str, DispatchRecord] = {}
        if self.state_path.exists():
            try:
                raw = json.loads(self.state_path.read_text())
                if not isinstance(raw, dict) or not isinstance(raw.get("records", {}), dict):
                    raise ControllerError("controller state must be an object containing records")
                self.records = {cid: DispatchRecord(**record) for cid, record in raw.get("records", {}).items()}
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ControllerError(f"invalid controller state: {exc}") from exc
            self._reconcile_dispatching()

    def _reconcile_dispatching(self) -> None:
        """Resolve records persisted between ``dispatching`` and process launch.

        A blind retry here can double-launch a campaign that is still running, and a
        blind failure burns an attempt that never happened.  Ask the backend which of
        the two actually occurred; only guess when it cannot tell us.
        """
        now = self.clock()
        launched = getattr(self.backend, "was_launched", None)
        for cid, record in list(self.records.items()):
            if record.state != "dispatching":
                continue
            evidence = None if launched is None else bool(launched(record.backend_job_id))
            if evidence is False:
                # The attempt never started, so it must not consume the retry budget.
                del self.records[cid]
                continue
            if evidence is True:
                # Adopt it as running; ``poll`` re-derives the truth from the state file,
                # the recorded PID and its start time.
                record.state = "running"
                record.message = "adopted after controller restart"
                continue
            record.state = "failed"
            record.message = "controller restarted during dispatch; refusing duplicate launch"
            record.next_retry_at = now + self._backoff(cid, record.attempt, record.started_at)

    def _persist(self, status: str) -> None:
        _atomic(self.state_path, {
            "records": {cid: asdict(record) for cid, record in sorted(self.records.items())},
            "status": status, "updated_at": self.clock(),
        })
        if self.portfolio.path is not None:
            self.portfolio.save()

    def _register_artifact(self, campaign_id: str, payload: Any) -> None:
        if not isinstance(payload, Mapping):
            raise ControllerError("artifact payload must be a mapping")
        artifact_id = payload.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id.strip():
            raise ControllerError("artifact_id must be a non-empty string")
        if artifact_id in self.portfolio.artifacts:
            raise ControllerError(f"artifact {artifact_id!r} already exists")
        campaign = self.portfolio.campaigns[campaign_id]
        if payload.get("producer_campaign_id") not in {None, campaign_id}:
            raise ControllerError("artifact producer does not match campaign")
        if payload.get("model_id") != campaign.model_id:
            raise ControllerError("artifact model_id does not match campaign model_id")
        allowed = {"model_id", "verdict", "dataset_manifest_hash", "split_manifest_hash",
                   "prediction_manifest_hash", "coverage", "input_artifact_ids"}
        values = {key: payload[key] for key in allowed if key in payload}
        self.portfolio.register_artifact(artifact_id, **values)

    def _poll_records(self, now: float) -> None:
        for cid, record in sorted(self.records.items()):
            if record.state != "running":
                continue
            try:
                polled = self.backend.poll(record.backend_job_id)
            except Exception as exc:
                polled = PollResult("failed", message=f"backend poll failed: {exc}")
            if (polled.attempt_token is not None
                    and polled.attempt_token != record.backend_job_id):
                polled = PollResult(
                    "failed",
                    message=f"result belongs to superseded attempt {polled.attempt_token!r}",
                )
            if polled.state == "completed":
                record.checkpoint_uri = polled.checkpoint_uri or record.checkpoint_uri
                try:
                    if polled.artifact is not None:
                        self._register_artifact(cid, polled.artifact)
                    record.state = "completed"
                except Exception as exc:
                    record.state = "failed"
                    record.message = f"artifact refused: {exc}"
                    record.next_retry_at = now + self._backoff(cid, record.attempt, record.started_at)
            elif polled.state in {"failed", "timed_out"}:
                record.state = "failed"
                record.checkpoint_uri = polled.checkpoint_uri or record.checkpoint_uri
                record.next_retry_at = now + self._backoff(cid, record.attempt, record.started_at)
                record.message = polled.message
            elif polled.state != "running":
                record.state = "failed"
                record.message = f"unknown backend state: {polled.state!r}"
                record.next_retry_at = now + self._backoff(cid, record.attempt, record.started_at)

    def _dispatch(self, job: JobSpec, now: float) -> bool:
        prior = self.records.get(job.campaign_id)
        attempt = (prior.attempt + 1) if prior else 1
        checkpoint_uri = prior.checkpoint_uri if prior else None
        if prior is not None:
            # Never race a still-live predecessor: it would write this campaign's
            # artifact underneath the new attempt.
            terminate = getattr(self.backend, "terminate", None)
            if terminate is not None:
                try:
                    terminate(prior.backend_job_id)
                except Exception:  # a dead predecessor is the normal case
                    pass
        token = f"{job.campaign_id}.attempt-{attempt}"
        self.records[job.campaign_id] = DispatchRecord(
            token, state="dispatching", attempt=attempt, started_at=now,
            checkpoint_uri=checkpoint_uri,
        )
        self._persist("dispatching")
        try:
            prepared = getattr(self.backend, "submit_prepared", None)
            backend_id = prepared(job, token) if prepared else self.backend.submit(job)
            self.records[job.campaign_id] = DispatchRecord(
                backend_id, attempt=attempt, started_at=now, checkpoint_uri=checkpoint_uri,
            )
            self._persist("running")
            return True
        except Exception as exc:
            record = self.records[job.campaign_id]
            record.state = "failed"
            record.message = f"dispatch failed: {exc}"
            record.next_retry_at = now + self._backoff(job.campaign_id, attempt, now)
            return False

    def tick(self) -> TickResult:
        now = self.clock()
        self._poll_records(now)

        states: dict[str, JobState] = {}
        for cid, record in self.records.items():
            if record.state == "failed" and now < record.next_retry_at:
                states[cid] = JobState(cid, "blocked", attempt=record.attempt,
                                       message=f"retry backoff until {record.next_retry_at:g}")
            else:
                states[cid] = JobState(cid, record.state, attempt=record.attempt,
                                       backend_job_id=record.backend_job_id,
                                       checkpoint_uri=record.checkpoint_uri,
                                       message=record.message)
        decision: ScheduleDecision | None = None
        blocked: dict[str, str] = {}
        try:
            decision = portfolio_schedule(
                self.portfolio, self.specs, states, self.capacity, max_active=self.max_active,
            )
        except Exception as exc:
            blocked = {"controller": str(exc)}
        started: list[str] = []
        if decision is not None:
            blocked = dict(decision.blocked)
            for job in decision.jobs:
                if self._dispatch(job, now):
                    started.append(job.campaign_id)

        completed = tuple(sorted(cid for cid, record in self.records.items() if record.state == "completed"))
        running = tuple(sorted(cid for cid, record in self.records.items() if record.state == "running"))
        live = any(record.state in LIVE_STATES for record in self.records.values())
        retry_waiting = any(
            record.state == "failed" and now < record.next_retry_at
            for record in self.records.values()
        )
        all_ids = {str(spec["id"]) for spec in self.specs}
        if decision is None and (live or started):
            # A scheduling failure is not evidence that the portfolio is finished; keep
            # supervising the executors that are still alive and surface the error.
            status = "running"
        elif set(completed) == all_ids:
            status = "complete"
        elif running or started:
            status = "running"
        elif retry_waiting:
            status = "waiting"
        else:
            status = "blocked"
        self._persist(status)
        return TickResult(status, tuple(started), running, completed, blocked)

    def _backoff(self, campaign_id: str, attempt: int, started_at: float | None = None) -> float:
        base = min(self.max_retry_backoff_seconds,
                   self.retry_backoff_seconds * (2 ** max(0, attempt - 1)))
        # Per-attempt jitter prevents synchronized restart storms; the seed is explicit
        # so a run remains replayable from its recorded attempt and start time.
        seed = repr((campaign_id, attempt, started_at))
        jitter = 0.9 + random.Random(seed).random() * 0.2
        return min(self.max_retry_backoff_seconds, base * jitter)

    def run(self, *, poll_interval: float = 10.0, one_shot: bool = False,
            sleeper=time.sleep) -> TickResult:
        if poll_interval <= 0:
            raise ControllerError("poll_interval must be positive")
        stopped = False
        previous = signal.getsignal(signal.SIGTERM)

        def stop(_signum, _frame):
            nonlocal stopped
            stopped = True

        signal.signal(signal.SIGTERM, stop)
        try:
            while True:
                result = self.tick()
                if one_shot or result.status in {"complete", "blocked"} or stopped:
                    return result
                sleeper(poll_interval)
        finally:
            signal.signal(signal.SIGTERM, previous)


class ExecutorProcessBackend:
    """Restart-safe adapter that delegates local work to executor_cli processes."""

    def __init__(self, root: str | Path, *, heartbeat_timeout_seconds: float = 30.0,
                 launch_timeout_seconds: float = LAUNCH_TIMEOUT_SECONDS):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.launch_timeout_seconds = launch_timeout_seconds
        self.processes: dict[str, subprocess.Popen] = {}

    @staticmethod
    def _campaign_of(job_id: str) -> str:
        return job_id.rsplit(".attempt-", 1)[0]

    def _log_path(self, job_id: str) -> Path:
        return self.root / "logs" / f"{job_id}.executor.log"

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def submit(self, job: JobSpec) -> str:
        return self.submit_prepared(job, f"{job.campaign_id}.attempt-1")

    def submit_prepared(self, job: JobSpec, job_id: str) -> str:
        job_path = self.root / f"{job_id}.job.json"
        state_path = self.root / f"{job_id}.state.json"
        artifact_path = Path(job.artifact_result_path) if job.artifact_result_path else None
        if artifact_path is not None:
            artifact_path.unlink(missing_ok=True)
        _atomic(job_path, asdict(job))
        log_path = self._log_path(job_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # The scheduler already validated the campaign's declared environment keys, so
        # the executor must be told to allow exactly those.
        allowed = sorted(set(LocalSubprocessBackend.DEFAULT_ENV_ALLOWLIST) | set(job.environment))
        command = [sys.executable, "-m", "knowledge.ml_registry.executor_cli", "run-job",
                   "--job", str(job_path), "--state", str(state_path),
                   "--log-dir", str(self.root / "logs")]
        for key in allowed:
            command += ["--allow-env", key]
        with log_path.open("ab") as log_stream:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=log_stream, stderr=log_stream,
                start_new_session=True,
            )
        self.processes[job_id] = process
        _atomic(self.root / f"{job_id}.process.json", {
            "pid": process.pid, "started_at": time.time(), "state_path": str(state_path),
            "start_token": process_probe.start_token(process.pid),
        })
        # Bind the campaign's artifact slot to this attempt so a resurrected
        # predecessor's result is refused rather than adopted.
        _atomic(self.root / f"{self._campaign_of(job_id)}.active.json", {"job_id": job_id})
        return job_id

    def was_launched(self, job_id: str) -> bool:
        """True when a process was actually started for this attempt token."""
        return ((self.root / f"{job_id}.process.json").exists()
                or (self.root / f"{job_id}.state.json").exists())

    def terminate(self, job_id: str, *, grace: float = 5.0) -> bool:
        """Stop a still-live attempt (and its executor's own child group)."""
        record = self._read_json(self.root / f"{job_id}.process.json")
        if record is None:
            return False
        killed = process_probe.terminate_group(
            record.get("pid"), expected=record.get("start_token"), grace=grace,
        )
        process = self.processes.get(job_id)
        if process is not None:
            try:
                process.wait(timeout=grace)
            except (subprocess.TimeoutExpired, OSError):
                pass
        return killed

    def _exit_code(self, job_id: str) -> int | None:
        """Reap our own child so a dead executor cannot masquerade as a live PID."""
        process = self.processes.get(job_id)
        return None if process is None else process.poll()

    def _log_tail(self, job_id: str, limit: int = 500) -> str:
        try:
            text = self._log_path(job_id).read_text(errors="replace")
        except OSError:
            return ""
        return text[-limit:].strip()

    def _failed(self, job_id: str, message: str) -> PollResult:
        return PollResult("failed", message=message, attempt_token=job_id)

    def poll(self, backend_job_id: str) -> PollResult:
        state_path = self.root / f"{backend_job_id}.state.json"
        record = self._read_json(self.root / f"{backend_job_id}.process.json")
        exit_code = self._exit_code(backend_job_id)
        alive = record is not None and process_probe.matches(
            record.get("pid"), record.get("start_token")
        )
        if not state_path.exists():
            if exit_code is not None:
                return self._failed(backend_job_id, (
                    f"executor exited with code {exit_code} before writing state: "
                    f"{self._log_tail(backend_job_id)}").strip())
            if not alive:
                return self._failed(
                    backend_job_id, "executor state missing and process is not alive")
            launched_at = record.get("started_at") if record else None
            if (isinstance(launched_at, (int, float))
                    and time.time() - launched_at > self.launch_timeout_seconds):
                return self._failed(backend_job_id, (
                    f"executor published no state within {self.launch_timeout_seconds:g}s "
                    "of launch"))
            return PollResult("running", attempt_token=backend_job_id)
        try:
            state = json.loads(state_path.read_text())
            if not isinstance(state, dict) or state.get("state") not in {
                "running", "completed", "failed", "timed_out"
            }:
                raise ValueError("unknown or malformed executor state")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return self._failed(backend_job_id, str(exc))
        if state["state"] == "running":
            if exit_code is not None:
                return self._failed(backend_job_id, (
                    f"executor exited with code {exit_code} while state was running: "
                    f"{self._log_tail(backend_job_id)}").strip())
            heartbeat = state.get("heartbeat_at")
            if (not isinstance(heartbeat, (int, float)) or isinstance(heartbeat, bool)
                    or time.time() - heartbeat > self.heartbeat_timeout_seconds):
                return self._failed(backend_job_id, "executor heartbeat is missing or stale")
            if not alive:
                return self._failed(
                    backend_job_id, "executor process died while state was running")
        elif state.get("artifact") is not None:
            active = self._read_json(self.root / f"{self._campaign_of(backend_job_id)}.active.json")
            if active is not None and active.get("job_id") != backend_job_id:
                return self._failed(backend_job_id, (
                    f"artifact was produced by superseded attempt {backend_job_id!r}; "
                    f"attempt {active.get('job_id')!r} is active"))
        return PollResult(
            str(state["state"]), artifact=state.get("artifact"), message=state.get("message"),
            checkpoint_uri=state.get("checkpoint_uri"), attempt_token=backend_job_id,
        )
