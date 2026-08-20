"""Persistent, dependency-safe controller for ML portfolio execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Protocol, Sequence

from knowledge.ml_registry.portfolio import CampaignStatus, Portfolio, PortfolioValidationError
from knowledge.ml_registry.scheduler import JobSpec, JobState, ResourceProfile, ScheduleDecision, schedule


MAX_ACTIVE_CAMPAIGNS = 2


class ControllerError(ValueError):
    pass


@dataclass(frozen=True)
class PollResult:
    state: str
    artifact: Mapping[str, Any] | None = None
    message: str | None = None
    checkpoint_uri: str | None = None


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
            # A dispatching record was persisted before process launch.  It may have
            # launched, so never blindly submit it again; fail it into normal retry.
            for record in self.records.values():
                if record.state == "dispatching":
                    record.state = "failed"
                    record.message = "controller restarted during dispatch; refusing duplicate launch"

    def _persist(self, status: str) -> None:
        _atomic(self.state_path, {
            "records": {cid: asdict(record) for cid, record in sorted(self.records.items())},
            "status": status, "updated_at": self.clock(),
        })
        if self.portfolio.path is not None:
            self.portfolio.save()

    def _register_artifact(self, campaign_id: str, payload: Mapping[str, Any]) -> None:
        artifact_id = str(payload.get("artifact_id", ""))
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

    def tick(self) -> TickResult:
        now = self.clock()
        for cid, record in sorted(self.records.items()):
            if record.state != "running":
                continue
            try:
                polled = self.backend.poll(record.backend_job_id)
            except Exception as exc:
                polled = PollResult("failed", message=f"backend poll failed: {exc}")
            if polled.state == "completed":
                try:
                    if polled.artifact:
                        self._register_artifact(cid, polled.artifact)
                    record.state = "completed"
                except (ControllerError, PortfolioValidationError, TypeError, ValueError) as exc:
                    record.state = "failed"
                    record.message = f"artifact refused: {exc}"
                    record.next_retry_at = now + self._backoff(cid, record.attempt)
            elif polled.state in {"failed", "timed_out"}:
                record.state = "failed"
                record.checkpoint_uri = polled.checkpoint_uri or record.checkpoint_uri
                record.next_retry_at = now + self._backoff(cid, record.attempt)
                record.message = polled.message
            elif polled.state != "running":
                record.state = "failed"
                record.message = f"unknown backend state: {polled.state!r}"
                record.next_retry_at = now + self._backoff(cid, record.attempt)

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
        try:
            decision = portfolio_schedule(
                self.portfolio, self.specs, states, self.capacity, max_active=self.max_active,
            )
        except Exception as exc:
            self._persist("blocked")
            return TickResult("blocked", (), (), (), {"controller": str(exc)})
        started: list[str] = []
        for job in decision.jobs:
            prior = self.records.get(job.campaign_id)
            attempt = (prior.attempt + 1) if prior else 1
            token = f"{job.campaign_id}.attempt-{attempt}"
            self.records[job.campaign_id] = DispatchRecord(
                token, state="dispatching", attempt=attempt, started_at=now,
            )
            self._persist("dispatching")
            try:
                prepared = getattr(self.backend, "submit_prepared", None)
                backend_id = prepared(job, token) if prepared else self.backend.submit(job)
                self.records[job.campaign_id] = DispatchRecord(
                    backend_id, attempt=attempt, started_at=now,
                )
                self._persist("running")
                started.append(job.campaign_id)
            except Exception as exc:
                record = self.records[job.campaign_id]
                record.state = "failed"
                record.message = f"dispatch failed: {exc}"
                record.next_retry_at = now + self._backoff(job.campaign_id, attempt)

        completed = tuple(sorted(cid for cid, record in self.records.items() if record.state == "completed"))
        running = tuple(sorted(cid for cid, record in self.records.items() if record.state == "running"))
        retry_waiting = any(
            record.state == "failed" and now < record.next_retry_at
            for record in self.records.values()
        )
        all_ids = {str(spec["id"]) for spec in self.specs}
        if set(completed) == all_ids:
            status = "complete"
        elif running or started:
            status = "running"
        elif retry_waiting:
            status = "waiting"
        else:
            status = "blocked"
        self._persist(status)
        return TickResult(status, tuple(started), running, completed, decision.blocked)

    def _backoff(self, campaign_id: str, attempt: int) -> float:
        base = min(self.max_retry_backoff_seconds,
                   self.retry_backoff_seconds * (2 ** max(0, attempt - 1)))
        # Stable jitter prevents synchronized restart storms while retaining replayability.
        jitter = 0.9 + (sum(campaign_id.encode("utf-8")) % 21) / 100
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
    def __init__(self, root: str | Path, *, heartbeat_timeout_seconds: float = 30.0):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds

    def submit(self, job: JobSpec) -> str:
        return self.submit_prepared(job, f"{job.campaign_id}.attempt-1")

    def submit_prepared(self, job: JobSpec, job_id: str) -> str:
        job_path = self.root / f"{job_id}.job.json"
        state_path = self.root / f"{job_id}.state.json"
        artifact_path = Path(job.artifact_result_path) if job.artifact_result_path else None
        if artifact_path is not None:
            artifact_path.unlink(missing_ok=True)
        _atomic(job_path, asdict(job))
        process = subprocess.Popen(
            [sys.executable, "-m", "knowledge.ml_registry.executor_cli", "run-job",
             "--job", str(job_path), "--state", str(state_path),
             "--log-dir", str(self.root / "logs")],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _atomic(self.root / f"{job_id}.process.json", {
            "pid": process.pid, "started_at": time.time(), "state_path": str(state_path),
        })
        return job_id

    def poll(self, backend_job_id: str) -> PollResult:
        state_path = self.root / f"{backend_job_id}.state.json"
        if not state_path.exists():
            process_path = self.root / f"{backend_job_id}.process.json"
            try:
                process = json.loads(process_path.read_text())
                os.kill(int(process["pid"]), 0)
                return PollResult("running")
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                return PollResult("failed", message="executor state missing and process is not alive")
        try:
            state = json.loads(state_path.read_text())
            if not isinstance(state, dict) or state.get("state") not in {
                "running", "completed", "failed", "timed_out"
            }:
                raise ValueError("unknown or malformed executor state")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return PollResult("failed", message=str(exc))
        if state["state"] == "running":
            heartbeat = state.get("heartbeat_at")
            if (not isinstance(heartbeat, (int, float)) or isinstance(heartbeat, bool)
                    or time.time() - heartbeat > self.heartbeat_timeout_seconds):
                return PollResult("failed", message="executor heartbeat is missing or stale")
            process_path = self.root / f"{backend_job_id}.process.json"
            try:
                process = json.loads(process_path.read_text())
                os.kill(int(process["pid"]), 0)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                return PollResult("failed", message="executor process died while state was running")
        return PollResult(
            str(state["state"]), artifact=state.get("artifact"), message=state.get("message"),
            checkpoint_uri=state.get("checkpoint_uri"),
        )
