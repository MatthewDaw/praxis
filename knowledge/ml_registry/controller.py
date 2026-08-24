"""Persistent, dependency-safe controller for ML portfolio execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Protocol, Sequence

from knowledge.ml_registry.contracts import ArtifactPin, LaunchIntent
from knowledge.ml_registry.portfolio import CampaignStatus, Portfolio, PortfolioValidationError
from knowledge.ml_registry.runtime import LeaseIntentCoordinator, ResourceConflict, StopReport
from knowledge.ml_registry.runtime.progress import ProgressSnapshot, read_latest_progress
from knowledge.ml_registry.scheduler import JobSpec, JobState, ResourceProfile, ScheduleDecision, schedule
from knowledge.ml_registry.services.readiness import explain_readiness
from knowledge.ml_registry.storage.registry import Registry


MAX_ACTIVE_CAMPAIGNS = 2


class ControllerError(ValueError):
    pass


@dataclass(frozen=True)
class PollResult:
    state: str
    artifact: Mapping[str, Any] | None = None
    message: str | None = None
    checkpoint_uri: str | None = None
    progress: ProgressSnapshot | None = None


class AsyncBackend(Protocol):
    def submit(self, job: JobSpec) -> str: ...
    def poll(self, backend_job_id: str) -> PollResult: ...
    def cancel(self, backend_job_id: str, *, force: bool) -> bool: ...


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


@dataclass(frozen=True)
class SlotStatus:
    campaign_id: str | None
    reason_code: str
    stage: str | None = None
    idea_id: str | None = None
    run_id: str | None = None
    latest_metric: float | None = None
    verdict: str | None = None
    progress: ProgressSnapshot | None = None
    heartbeat_age_seconds: float | None = None
    lease: Mapping[str, Any] | None = None
    retry_state: str | None = None
    blocker: str | None = None


@dataclass(frozen=True)
class ControllerStatus:
    slots: tuple[SlotStatus, ...]
    ready_frontier: tuple[str, ...]
    dependency_waits: Mapping[str, str] | None = None


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
    registry: Registry | None = None,
) -> ScheduleDecision:
    """Schedule only READY campaigns whose exact artifact contracts remain current."""
    if not 1 <= max_active <= MAX_ACTIVE_CAMPAIGNS:
        raise ControllerError(f"max_active must be between 1 and {MAX_ACTIVE_CAMPAIGNS}")
    gated: dict[str, JobState | Mapping[str, Any]] = dict(states)
    artifact_pins: dict[str, tuple[ArtifactPin, ...]] = {}
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
        if registry is not None:
            artifact_readiness = explain_readiness(registry, campaign_id)
            if not artifact_readiness.ready:
                gated[campaign_id] = JobState(
                    campaign_id, "blocked", message=artifact_readiness.reason,
                )
            else:
                artifact_pins[campaign_id] = artifact_readiness.pins
            continue
        readiness = portfolio.refresh(campaign_id)
        campaign = portfolio.campaigns[campaign_id]
        if campaign.status != CampaignStatus.READY or campaign.stale or not readiness.activatable:
            reasons = list(readiness.reasons)
            missing = [dependency for dependency in campaign.dependencies
                       if dependency.artifact_id not in portfolio.artifacts]
            if missing:
                producers = {candidate.model_id: candidate.id
                             for candidate in portfolio.campaigns.values()}
                reasons = [f"waiting on {producers.get(item.upstream_model_id, item.upstream_model_id)}:"
                           f"{item.artifact_id}" for item in missing]
            if campaign.status != CampaignStatus.READY:
                reasons.insert(0, f"campaign status is {campaign.status.value}, expected READY")
            gated[campaign_id] = JobState(campaign_id, "blocked", message="; ".join(reasons))
    decision = schedule(
        campaign_specs, gated, capacity, max_concurrency=max_active,
        remaining_cost=remaining_cost,
    )
    jobs = tuple(replace(job, artifact_pins=artifact_pins.get(job.campaign_id, ()))
                 for job in decision.jobs)
    return ScheduleDecision(jobs, decision.blocked, decision.available)


class PortfolioController:
    def __init__(self, *, portfolio: Portfolio, campaign_specs: Sequence[Mapping[str, Any]],
                 capacity: ResourceProfile | Mapping[str, Any], backend: AsyncBackend,
                 state_path: str | Path, max_active: int = MAX_ACTIVE_CAMPAIGNS,
                 retry_backoff_seconds: float = 60.0, max_retry_backoff_seconds: float = 3600.0,
                 clock=time.time, coordinator: LeaseIntentCoordinator | None = None,
                 lease_factory=None, completion_verifier=None, run_superseder=None,
                 failpoint=None, registry: Registry | None = None):
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
        self.coordinator = coordinator
        self.lease_factory = lease_factory
        self.completion_verifier = completion_verifier
        self.run_superseder = run_superseder
        self.registry = registry
        self.failpoint = failpoint or (lambda _boundary, _campaign_id: None)
        self.admission_stopped = False
        self._last_blocked: dict[str, str] = {}
        self.records: dict[str, DispatchRecord] = {}
        if self.state_path.exists():
            try:
                raw = json.loads(self.state_path.read_text())
                if not isinstance(raw, dict) or not isinstance(raw.get("records", {}), dict):
                    raise ControllerError("controller state must be an object containing records")
                self.records = {cid: DispatchRecord(**record) for cid, record in raw.get("records", {}).items()}
                blocked = raw.get("blocked", {})
                if isinstance(blocked, dict) and all(isinstance(key, str) and isinstance(value, str)
                                                     for key, value in blocked.items()):
                    self._last_blocked = dict(blocked)
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ControllerError(f"invalid controller state: {exc}") from exc
            for record in self.records.values():
                if record.state == "dispatching":
                    reconcile = getattr(self.backend, "reconcile", None)
                    result = reconcile(record.backend_job_id) if reconcile else None
                    if result is not None:
                        record.state, record.message = result.state, result.message
                    else:
                        record.state = "failed"
                        record.message = "unresolved launch intent; duplicate launch refused"

    def _persist(self, status: str) -> None:
        _atomic(self.state_path, {
            "records": {cid: asdict(record) for cid, record in sorted(self.records.items())},
            "blocked": dict(sorted(self._last_blocked.items())),
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
        self._last_blocked = {}
        for cid, record in sorted(self.records.items()):
            if record.state != "running":
                continue
            try:
                polled = self.backend.poll(record.backend_job_id)
            except Exception as exc:
                polled = PollResult("failed", message=f"backend poll failed: {exc}")
            if polled.state == "completed":
                try:
                    self.failpoint("outcome_written", cid)
                    artifact = polled.artifact
                    if self.completion_verifier is not None:
                        self.failpoint("finalizing", cid)
                        artifact = self.completion_verifier(cid, polled)
                    # Registry-native readiness resolves the producer's immutable
                    # version/artifact directly.  The compatibility Portfolio view
                    # is membership-only and must not become a second artifact store.
                    if artifact and self.registry is None:
                        self._register_artifact(cid, artifact)
                    record.state = "completed"
                    self.failpoint("finalized_before_lease_release", cid)
                    if self.coordinator is not None:
                        self.coordinator.release(cid)
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
                registry=self.registry,
            )
        except Exception as exc:
            self._persist("blocked")
            return TickResult("blocked", (), (), (), {"controller": str(exc)})
        started: list[str] = []
        for job in (() if self.admission_stopped else decision.jobs):
            prior = self.records.get(job.campaign_id)
            attempt = (prior.attempt + 1) if prior else 1
            token = f"{job.campaign_id}.attempt-{attempt}"
            self.records[job.campaign_id] = DispatchRecord(
                token, state="dispatching", attempt=attempt, started_at=now,
            )
            if self.coordinator is not None and self.lease_factory is not None:
                try:
                    lease = self.lease_factory(job, token)
                    self.coordinator.acquire(lease)
                    digest = hashlib.sha256(json.dumps(asdict(job), sort_keys=True).encode()).hexdigest()
                    self.coordinator.prepare(LaunchIntent(
                        LaunchIntent.VERSION, token, job.campaign_id, attempt, digest,
                        (lease.lease_id,), lease.owner, "prepared", now,
                    ))
                    self.failpoint("intent_written", job.campaign_id)
                except ResourceConflict as exc:
                    self.records.pop(job.campaign_id, None)
                    self._last_blocked[job.campaign_id] = exc.reason_code
                    continue
            self._persist("dispatching")
            try:
                prepared = getattr(self.backend, "submit_prepared", None)
                backend_id = prepared(job, token) if prepared else self.backend.submit(job)
                self.records[job.campaign_id] = DispatchRecord(
                    backend_id, attempt=attempt, started_at=now,
                )
                self._persist("running")
                self.failpoint("process_spawned", job.campaign_id)
                self.failpoint("arm_running", job.campaign_id)
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
        blocked = dict(decision.blocked)
        blocked.update(self._last_blocked)
        self._last_blocked = blocked
        self._persist(status)
        return TickResult(status, tuple(started), running, completed, blocked)

    def status(self) -> ControllerStatus:
        running = tuple(sorted(cid for cid, record in self.records.items()
                               if record.state == "running"))
        slots = []
        for cid in running[:self.max_active]:
            record = self.records[cid]
            try:
                polled = self.backend.poll(record.backend_job_id)
            except Exception:
                polled = PollResult("running")
            lease = None
            if self.coordinator is not None and cid in self.coordinator.leases:
                lease = self.coordinator.leases[cid].to_mapping()
            heartbeat_age = None
            heartbeat_reader = getattr(self.backend, "heartbeat_age", None)
            if heartbeat_reader is not None:
                heartbeat_age = heartbeat_reader(record.backend_job_id)
            slots.append(SlotStatus(
                cid, "occupied", progress=polled.progress,
                heartbeat_age_seconds=heartbeat_age, lease=lease,
                retry_state=(None if record.attempt <= 1 else f"attempt {record.attempt}"),
                blocker=record.message,
            ))
        reason_values = list(dict.fromkeys(self._last_blocked.values()))
        reason_values.sort(key=lambda reason: (
            1 if reason.startswith(("waiting ", "campaign status", "concurrency")) else 0,
            reason,
        ))
        reasons = iter(reason_values)
        while len(slots) < self.max_active:
            slots.append(SlotStatus(None, next(reasons, "no_ready_campaign")))
        ready = tuple(sorted(str(spec["id"]) for spec in self.specs
                             if self.records.get(str(spec["id"])) is None
                             or self.records[str(spec["id"])].state
                             not in {"running", "completed"}))
        return ControllerStatus(tuple(slots), ready, dict(self._last_blocked))

    def stop(self, *, mode: str, sleeper=time.sleep) -> StopReport:
        if mode not in {"force", "drain"}:
            raise ControllerError("stop mode must be force or drain")
        self.admission_stopped = True
        active = {cid: record for cid, record in self.records.items()
                  if record.state in {"dispatching", "running"}}
        graceful: set[str] = set()
        forced: set[str] = set()
        retryable: set[str] = set()
        orphaned: set[str] = set()
        if mode == "drain":
            while active:
                for cid, record in list(active.items()):
                    result = self.backend.poll(record.backend_job_id)
                    if result.state == "running":
                        continue
                    if result.state == "completed":
                        try:
                            artifact = (self.completion_verifier(cid, result)
                                        if self.completion_verifier is not None else result.artifact)
                            if artifact:
                                self._register_artifact(cid, artifact)
                            record.state = "completed"
                            graceful.add(cid)
                        except (ControllerError, PortfolioValidationError, TypeError, ValueError) as exc:
                            record.state = "failed"
                            record.message = f"artifact refused: {exc}"
                            retryable.add(cid)
                    else:
                        record.state = "failed"
                        retryable.add(cid)
                    if self.coordinator is not None:
                        self.coordinator.release(cid)
                    del active[cid]
                if active:
                    sleeper(0.02)
        else:
            for cid, record in active.items():
                cancel = getattr(self.backend, "cancel", None)
                if cancel is not None and cancel(record.backend_job_id, force=True):
                    forced.add(cid)
                else:
                    orphaned.add(cid)
                record.state = "failed"
                record.message = "operator force stop"
                retryable.add(cid)
                if self.run_superseder is not None:
                    self.run_superseder(cid, "operator force stop")
                if self.coordinator is not None:
                    self.coordinator.release(cid)
        self._persist("stopped")
        return StopReport(frozenset(graceful), frozenset(forced),
                          frozenset(retryable), frozenset(orphaned))

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
    _PRAXIS_ROOT = Path(__file__).resolve().parents[2]

    def __init__(self, root: str | Path, *, heartbeat_timeout_seconds: float = 30.0,
                 coordinator: LeaseIntentCoordinator | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.coordinator = coordinator
        self._processes: dict[str, subprocess.Popen[bytes]] = {}

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
            cwd=self._PRAXIS_ROOT,
        )
        self._processes[job_id] = process
        _atomic(self.root / f"{job_id}.process.json", {
            "pid": process.pid, "pgid": process.pid, "started_at": time.time(),
            "state_path": str(state_path),
        })
        if self.coordinator is not None and job_id in self.coordinator.intents:
            self.coordinator.transition(job_id, state="spawned", pid=process.pid, pgid=process.pid)
        return job_id

    def reconcile(self, backend_job_id: str) -> PollResult | None:
        process_path = self.root / f"{backend_job_id}.process.json"
        if not process_path.exists():
            return None
        return self.poll(backend_job_id)

    def cancel(self, backend_job_id: str, *, force: bool) -> bool:
        try:
            process = json.loads((self.root / f"{backend_job_id}.process.json").read_text())
            pgid = int(process["pgid"])
            os.killpg(pgid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            return True
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return False
        owned = self._processes.get(backend_job_id)
        if owned is not None:
            try:
                owned.wait(timeout=5)
                return True
            except subprocess.TimeoutExpired:
                return False
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                pass
            # A reconstructed backend has no Popen handle with which to reap its
            # executor. Linux reports a killed zombie group as present, while Darwin
            # may report EPERM. In both cases the zombie owns no executable child and
            # cancellation is complete rather than orphaned.
            checked = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(process.get("pid", pgid))],
                capture_output=True, text=True, check=False,
            )
            states = checked.stdout.split()
            if checked.returncode != 0 or not states or all(state.startswith("Z") for state in states):
                return True
            time.sleep(0.02)
        return False

    def poll(self, backend_job_id: str) -> PollResult:
        state_path = self.root / f"{backend_job_id}.state.json"
        if not state_path.exists():
            # Popen.poll() performs waitpid(WNOHANG): unlike os.kill(pid, 0), it
            # reaps an owned child and does not misclassify a dead zombie as a
            # live executor. The executor may fail before it can publish state
            # (for example an import/bootstrap error), which must be terminal.
            owned = self._processes.get(backend_job_id)
            if owned is not None:
                returncode = owned.poll()
                if returncode is not None:
                    return PollResult(
                        "failed",
                        message=("executor exited before publishing state "
                                 f"(returncode {returncode})"),
                    )
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
        elif backend_job_id in self._processes:
            self._processes[backend_job_id].wait(timeout=1)
        progress = None
        stdout_log = state.get("stdout_log")
        if isinstance(stdout_log, str):
            progress = read_latest_progress(stdout_log)
        return PollResult(
            str(state["state"]), artifact=state.get("artifact"), message=state.get("message"),
            checkpoint_uri=state.get("checkpoint_uri"), progress=progress,
        )

    def heartbeat_age(self, backend_job_id: str) -> float | None:
        try:
            state = json.loads((self.root / f"{backend_job_id}.state.json").read_text())
            heartbeat = state.get("heartbeat_at")
            if isinstance(heartbeat, (int, float)) and not isinstance(heartbeat, bool):
                return max(0.0, time.time() - float(heartbeat))
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return None
