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
                 retry_backoff_seconds: float = 60.0, clock=time.time):
        if not 1 <= max_active <= MAX_ACTIVE_CAMPAIGNS:
            raise ControllerError(f"max_active must be between 1 and {MAX_ACTIVE_CAMPAIGNS}")
        self.portfolio = portfolio
        self.specs = list(campaign_specs)
        self.capacity = capacity
        self.backend = backend
        self.state_path = Path(state_path)
        self.max_active = max_active
        self.retry_backoff_seconds = retry_backoff_seconds
        self.clock = clock
        self.records: dict[str, DispatchRecord] = {}
        if self.state_path.exists():
            raw = json.loads(self.state_path.read_text())
            self.records = {cid: DispatchRecord(**record) for cid, record in raw.get("records", {}).items()}

    def _persist(self, status: str) -> None:
        _atomic(self.state_path, {
            "records": {cid: asdict(record) for cid, record in sorted(self.records.items())},
            "status": status, "updated_at": self.clock(),
        })
        if self.portfolio.path is not None:
            self.portfolio.save()

    def _register_artifact(self, payload: Mapping[str, Any]) -> None:
        artifact_id = str(payload.get("artifact_id", ""))
        if artifact_id in self.portfolio.artifacts:
            return
        values = dict(payload)
        values.pop("artifact_id", None)
        self.portfolio.register_artifact(artifact_id, **values)

    def tick(self) -> TickResult:
        now = self.clock()
        for cid, record in sorted(self.records.items()):
            if record.state != "running":
                continue
            polled = self.backend.poll(record.backend_job_id)
            if polled.state == "completed":
                record.state = "completed"
                if polled.artifact:
                    self._register_artifact(polled.artifact)
            elif polled.state in {"failed", "timed_out"}:
                record.state = "failed"
                record.next_retry_at = now + self.retry_backoff_seconds * record.attempt
                record.message = polled.message

        states: dict[str, JobState] = {}
        for cid, record in self.records.items():
            if record.state == "failed" and now < record.next_retry_at:
                states[cid] = JobState(cid, "blocked", attempt=record.attempt,
                                       message=f"retry backoff until {record.next_retry_at:g}")
            else:
                states[cid] = JobState(cid, record.state, attempt=record.attempt,
                                       backend_job_id=record.backend_job_id, message=record.message)
        decision = portfolio_schedule(
            self.portfolio, self.specs, states, self.capacity, max_active=self.max_active,
        )
        started: list[str] = []
        for job in decision.jobs:
            prior = self.records.get(job.campaign_id)
            attempt = (prior.attempt + 1) if prior else 1
            backend_id = self.backend.submit(job)
            self.records[job.campaign_id] = DispatchRecord(backend_id, attempt=attempt)
            started.append(job.campaign_id)

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
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def submit(self, job: JobSpec) -> str:
        job_id = job.campaign_id
        job_path = self.root / f"{job_id}.job.json"
        state_path = self.root / f"{job_id}.state.json"
        _atomic(job_path, asdict(job))
        subprocess.Popen(
            [sys.executable, "-m", "knowledge.ml_registry.executor_cli", "run-job",
             "--job", str(job_path), "--state", str(state_path),
             "--log-dir", str(self.root / "logs")],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return job_id

    def poll(self, backend_job_id: str) -> PollResult:
        state_path = self.root / f"{backend_job_id}.state.json"
        if not state_path.exists():
            return PollResult("running")
        state = json.loads(state_path.read_text())
        return PollResult(str(state["state"]), message=state.get("message"))
