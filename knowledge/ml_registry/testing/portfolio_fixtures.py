"""Thin builders around production portfolio, registry, and runtime services."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

from knowledge.ml_registry import Registry
from knowledge.ml_registry.contracts import CampaignLease
from knowledge.ml_registry.controller import ExecutorProcessBackend, PollResult, PortfolioController
from knowledge.ml_registry.domain import CampaignBinding, CampaignView, IdeaInventory
from knowledge.ml_registry.portfolio import ArtifactDependency, CampaignStatus, Portfolio
from knowledge.ml_registry.runtime import (
    CampaignFinalization,
    LeaseIntentCoordinator,
    ProgressTracker,
    RegistryCompletionVerifier,
)
from knowledge.ml_registry.services.registry_aliases import adopt_run_and_promote
from knowledge.ml_registry.services.registry_aliases import invalidate_adoption
from knowledge.ml_registry.services.registry_finalize import RegistryFinalizer
from knowledge.ml_registry.services.registry_finalize import RegistryFinalizeService
from knowledge.ml_registry.services.registry_runs import complete_run
from knowledge.ml_registry.services.registry_runs import supersede_run
from knowledge.ml_registry.storage import RegistryError


REPO = Path(__file__).resolve().parents[3]
SHA = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], check=True,
                     capture_output=True, text=True).stdout.strip()


class _ControlledBackend:
    def __init__(self) -> None:
        self.results: dict[str, PollResult] = {}

    def submit(self, job) -> str:
        self.results[job.campaign_id] = PollResult("running")
        return job.campaign_id

    def poll(self, backend_job_id: str) -> PollResult:
        return self.results[backend_job_id]

    def cancel(self, backend_job_id: str, *, force: bool) -> bool:
        self.results[backend_job_id] = PollResult("failed", message="cancelled")
        return True


def _run(registry: Registry, run_id: str, *, idea_id: str = "idea-1") -> None:
    registry.create_run(
        run_id=run_id, experiment_id="R1", idea_id=idea_id, stage="representation",
        family="linear", params={}, metrics={}, code_ref={"schema_version": 1,
        "repo": str(REPO), "sha": SHA, "base_sha": SHA, "diff_hash": "d" * 64,
        "diff_lines": 1}, device_fingerprint="cpu:test", status="running", verdict=None,
        started_at=1, finished_at=None, claim_owner="controller", heartbeat_at=1,
    )


def _registry(root: Path) -> tuple[Registry, CampaignView, RegistryFinalizer]:
    registry = Registry(root)
    registry.create_experiment(experiment_id="R1", spec_digest="a" * 64,
        stages=["representation"], metric="f1", direction="maximize",
        win_condition={"metric_at_least": .5}, noise_floor=.01, baseline_throughput=1)
    registry.register_model(model_id="R1", family="linear", sport_scope="shared", axis="a01",
                            protocol="Detector", extends=None)
    _run(registry, "run-R1")
    complete_run(registry, run_id="run-R1", metrics={"metric": .8, "validity": "valid",
        "throughput": 1, "throughput_unit": "rows_per_second", "memory_gb": .1,
        "cpu_time": 1, "load": {"start_1m": 0, "end_1m": 0}})
    artifact = registry.create_artifact(run_id="run-R1", kind="checkpoint",
                                        content=b"fixture-model", schema_version="1")
    adopt_run_and_promote(registry, run_id="run-R1", model_id="R1", reason="fixture winner",
        model_version={"version": 1, "artifact_id": artifact, "checksum": artifact,
        "family_version": "linear@1", "code_sha": SHA, "preprocessing_hash": "prep",
        "calibration": {}, "thresholds": {},
        "compat_result": {"head_sha": SHA, "passed": True, "at": 1}, "status": "active"})
    row = registry.rows("runs")[0]
    view = CampaignView(CampaignBinding("R1", "R1", "fact-R1"),
        registry.rows("experiments")[0], registry.rows("registered_models")[0],
        SimpleNamespace(id="fact-R1"),
        (IdeaInventory(SimpleNamespace(id="idea-1"), "idea-1", "representation", (), (row,)),))
    finalizer = RegistryFinalizer(
        registry, compatibility_loader=lambda _version, path, _head: path.read_bytes() == b"fixture-model",
        min_measured=1,
    )
    return registry, view, finalizer


def _lease(job, token, *, conflict: str | None = None) -> CampaignLease:
    now = time.time()
    if conflict == "shared_cuda_0":
        lane, device, throughput = "gpu", "cuda:0", False
    elif conflict == "exclusive_cpu_throughput":
        lane, device, throughput = "cpu", "cpu", True
    else:
        lane, device, throughput = (("gpu", "cuda:0", False) if job.campaign_id == "R1"
                                    else ("cpu", f"cpu:{job.campaign_id}", False))
    return CampaignLease(1, f"lease:{job.campaign_id}", job.campaign_id, token, lane, device,
                         True, 1, "forbid", throughput, f"state/{job.campaign_id}",
                         f"checkout/{job.campaign_id}", f"cache/{job.campaign_id}",
                         f"ledger/{job.campaign_id}", now, now + 60)


class _Scenario:
    def __init__(self, root: Path, *, conflict: str | None = None, real: bool = False) -> None:
        self.root = Path(root)
        self.registry, self.view, self.finalizer = _registry(self.root / "registry")
        self.coordinator = LeaseIntentCoordinator(self.root / "ownership.json")
        self.backend = (ExecutorProcessBackend(self.root / "dispatch", coordinator=self.coordinator)
                        if real else _ControlledBackend())
        self.portfolio = Portfolio()
        self.portfolio.add_campaign("R1", "R1").status = CampaignStatus.READY
        self.portfolio.add_campaign("R2", "R2").status = CampaignStatus.READY
        version = self.registry.rows("model_versions")[0]
        dependency = ArtifactDependency("R1", "fit", "adopted", version["checksum"], "prep",
                                        version["checksum"], 1)
        self.portfolio.add_campaign("C1", "C1", [dependency]).status = CampaignStatus.READY
        command = ([sys.executable, "-c", "import time; time.sleep(.1)"] if real else ["fixture"])
        specs = [{"id": cid, "command": command, "resources": {"cpus": 1}}
                 for cid in ("R1", "R2", "C1")]
        self.completion = RegistryCompletionVerifier({
            "R1": CampaignFinalization(self.view, self.finalizer, 1, "fit")
        })
        self.controller = PortfolioController(
            portfolio=self.portfolio, campaign_specs=specs, capacity={"cpus": 3, "ram_gb": 8},
            backend=self.backend, state_path=self.root / "controller.json",
            coordinator=self.coordinator,
            lease_factory=lambda job, token: _lease(job, token, conflict=conflict),
            completion_verifier=lambda cid, result: self.completion(cid, result),
        )
        self.progress = ProgressTracker()
        self.refusal = SimpleNamespace(reason_code="")
        self.new_arms_started_after_stop = 0

    @property
    def ready_campaign_ids(self):
        return self.controller.status().ready_frontier

    def lease(self, campaign_id):
        return self.coordinator.leases[campaign_id]

    def lease_owner(self, campaign_id):
        lease = self.coordinator.leases.get(campaign_id)
        return lease.owner if lease else None

    def complete_with_production_alias(self, campaign_id, *, artifact_type):
        assert campaign_id == "R1" and artifact_type == "fit"
        self.finalizer.finalize(self.view, version=1, reason="fixture release")
        self.backend.results["R1"] = PollResult("completed", artifact={"outcome": "COMPLETE"})

    def finalize_verifications(self, campaign_id):
        return self.completion.verifications.get(campaign_id, 0)

    def attempt_overlapping_trials(self, *, idea_id):
        # The fixture registry already contains R1's completed run; create one live run,
        # then prove the canonical writer refuses a concurrent sibling.
        _run(self.registry, "live-arm", idea_id=idea_id)
        self.progress.started("R1")
        try:
            _run(self.registry, "overlap", idea_id=idea_id)
        except RegistryError as exc:
            self.refusal = SimpleNamespace(reason_code=str(exc))

    def registry_events(self, event_type, **fields):
        return tuple(event for event in self.registry.list_events()
                     if event.event_type == event_type
                     and all(event.payload.get(key) == value for key, value in fields.items()))


def three_node_scenario(tmp_path):
    return _Scenario(tmp_path)


def serial_arm_scenario(tmp_path):
    return _Scenario(tmp_path)


class _InvalidCompletionScenario(_Scenario):
    def __init__(self, root: Path, *, claim: str) -> None:
        super().__init__(root)
        self.claim = claim

    def finish_root_and_poll(self):
        self.controller.tick()
        artifact = {"outcome": "COMPLETE"}
        if self.claim == "missing_outcome":
            artifact = None
        elif self.claim == "missing_production_alias":
            pass
        elif self.claim == "checksum_mismatch":
            self.finalizer.finalize(self.view, version=1, reason="fixture release")
            artifact_id = self.registry.rows("model_versions")[0]["artifact_id"]
            self.registry.blobs.path(artifact_id).write_bytes(b"tampered fixture bytes")
        elif self.claim == "superseded_lineage":
            self.finalizer.finalize(self.view, version=1, reason="fixture release")
            invalidate_adoption(self.registry, {
                "model_id": "R1", "invalidated_version": 1, "parent_version": 1,
                "adoption_run_id": "run-R1", "evidence_run_ids": ["fixture"],
                "invalidated_lineage_id": "R1@1", "requeue_idea_ids": [],
                "reason": "fixture superseded lineage",
            })
        else:
            raise ValueError(f"unknown invalid completion claim {self.claim!r}")
        self.backend.results["R1"] = PollResult("completed", artifact=artifact)
        return self.controller.tick()

    def record(self, campaign_id):
        return self.controller.records[campaign_id]


def invalid_completion_scenario(tmp_path, *, claim):
    return _InvalidCompletionScenario(tmp_path, claim=claim)


def _campaign_spec(campaign_id, *, requires=(), schema_version="1"):
    return {
        "schema_version": 1, "campaign_id": campaign_id,
        "model_id_policy": campaign_id, "axis": "fixture", "sport_scope": ["shared"],
        "target_ontology": "fixture", "metric": {"name": "f1", "direction": "maximize"},
        "stages": [{"name": "representation"}], "corpora": [{"id": "fixture"}],
        "requires": list(requires),
        "produces": [{"artifact_type": "checkpoint", "schema_version": schema_version,
                      "oof_for": []}],
        "supervision": {"mode": "composing"}, "resources": {"lane": "cpu"},
        "isolation": {"state_root": campaign_id},
        "production": {"protocol": "Fixture"}, "extends": [],
        "deterministic_incumbent": None, "learned_escalation": False,
    }


class _PromotedArtifactScenario:
    def __init__(self, root: Path) -> None:
        self.registry, _view, _finalizer = _registry(Path(root) / "registry")
        self.fit_artifact_id = self.registry.rows("model_versions")[0]["artifact_id"]
        RegistryFinalizeService(self.registry).move_production(
            model_id="R1", version=1, reason="fixture production v1",
        )
        self.registry.register_campaign_spec(_campaign_spec("R1"))
        self.registry.register_campaign_spec(_campaign_spec("C1", requires=[{
            "artifact_type": "checkpoint", "producer_campaign_id": "R1",
            "min_coverage": 1.0, "oof": False,
        }]))

    def apply(self, mutation: str) -> None:
        if mutation == "tamper_bytes":
            self.registry.blobs.path(self.fit_artifact_id).write_bytes(b"tampered")
            return
        if mutation == "backdate_manifest":
            # The manifest advanced while production still carries the older schema.
            self.registry.register_campaign_spec(_campaign_spec("R1", schema_version="2"))
            return
        if mutation == "supersede_trial":
            _run(self.registry, "run-R1-v2", idea_id="idea-2")
            complete_run(self.registry, run_id="run-R1-v2", metrics={
                "metric": .81, "validity": "valid", "throughput": 1,
                "throughput_unit": "rows_per_second", "memory_gb": .1,
                "cpu_time": 1, "load": {"start_1m": 0, "end_1m": 0},
            })
            artifact = self.registry.create_artifact(
                run_id="run-R1-v2", kind="checkpoint", content=b"fixture-model-v2",
                schema_version="1",
            )
            adopt_run_and_promote(
                self.registry, run_id="run-R1-v2", model_id="R1", reason="fixture v2",
                model_version={"version": 2, "artifact_id": artifact, "checksum": artifact,
                    "family_version": "linear@1", "code_sha": SHA,
                    "preprocessing_hash": "prep", "calibration": {}, "thresholds": {},
                    "compat_result": {"head_sha": SHA, "passed": True, "at": 2},
                    "status": "active"},
            )
            RegistryFinalizeService(self.registry).move_production(
                model_id="R1", version=2, reason="fixture production v2",
            )
            invalidate_adoption(self.registry, {
                "model_id": "R1", "invalidated_version": 2, "parent_version": 1,
                "adoption_run_id": "run-R1-v2", "evidence_run_ids": ["fixture"],
                "invalidated_lineage_id": "R1@2", "requeue_idea_ids": [],
                "reason": "fixture superseded lineage",
            })
            self.fit_artifact_id = artifact
            return
        raise ValueError(f"unknown fixture mutation {mutation!r}")


def promoted_artifact_scenario(tmp_path):
    return _PromotedArtifactScenario(tmp_path)


def resource_conflict_scenario(tmp_path, *, conflict):
    return _Scenario(tmp_path, conflict=conflict)


class _ProcessScenario:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.registry = Registry(self.root / "registry")
        for cid in ("R1", "R2"):
            self.registry.create_experiment(experiment_id=cid, spec_digest="a" * 64,
                stages=["representation"], metric="f1", direction="maximize",
                win_condition={"metric_at_least": .5}, noise_floor=.01, baseline_throughput=1)
            _create_named_run(self.registry, cid)
        self.coordinator = LeaseIntentCoordinator(self.root / "ownership.json")
        self.backend = ExecutorProcessBackend(self.root / "dispatch", coordinator=self.coordinator)
        portfolio = Portfolio()
        for cid in ("R1", "R2"):
            portfolio.add_campaign(cid, cid).status = CampaignStatus.READY
        specs = [{"id": cid, "command": [sys.executable, "-c", "import time; time.sleep(.15)"],
                  "resources": {"cpus": 1}, "timeout_minutes": 1} for cid in ("R1", "R2")]
        self.controller = PortfolioController(
            portfolio=portfolio, campaign_specs=specs, capacity={"cpus": 3, "ram_gb": 8},
            backend=self.backend, state_path=self.root / "controller.json",
            coordinator=self.coordinator, lease_factory=_lease,
            completion_verifier=self._complete_run,
            run_superseder=lambda cid, reason: supersede_run(
                self.registry, run_id=f"run-{cid}", reason=reason,
            ),
        )
        self.new_arms_started_after_stop = 0

    def _complete_run(self, cid, _result):
        complete_run(self.registry, run_id=f"run-{cid}", metrics={"metric": .8,
            "validity": "valid", "throughput": 1, "throughput_unit": "rows_per_second",
            "memory_gb": .1, "cpu_time": 1, "load": {"start_1m": 0, "end_1m": 0}})
        return None

    def all_owned_process_groups_dead(self):
        for process in self.backend._processes.values():
            try:
                os.killpg(process.pid, 0)
            except (ProcessLookupError, PermissionError):
                continue
            return False
        return True

    def active_leases(self):
        return tuple(self.coordinator.leases.values())

    def all_in_flight_trials_superseded(self, *, reason):
        rows = self.registry.rows("runs")
        events = [event for event in self.registry.list_events()
                  if event.event_type == "run_superseded" and event.payload.get("reason") == reason]
        return all(row["status"] == "superseded" for row in rows) and len(events) == 2


def _create_named_run(registry: Registry, cid: str) -> None:
    registry.create_run(
        run_id=f"run-{cid}", experiment_id=cid, idea_id="idea-1", stage="representation",
        family="linear", params={}, metrics={}, code_ref={"schema_version": 1,
        "repo": str(REPO), "sha": SHA, "base_sha": SHA, "diff_hash": "d" * 64,
        "diff_lines": 1}, device_fingerprint="cpu:test", status="running", verdict=None,
        started_at=1, finished_at=None, claim_owner="controller", heartbeat_at=1,
    )


def two_root_process_scenario(tmp_path):
    return _ProcessScenario(tmp_path)


class _RestartScenario(_Scenario):
    def __init__(self, root: Path, *, crash_after: str) -> None:
        self.crash_after = crash_after
        super().__init__(root, real=True)
        self.controller.failpoint = self._fail

    def _fail(self, boundary, campaign_id):
        if campaign_id == "R1" and boundary == self.crash_after:
            raise RuntimeError(f"fixture crash after {boundary}")

    def run_until_crash(self):
        try:
            self.controller.tick()
            if self.crash_after in {"outcome_written", "finalizing", "finalized_before_lease_release"}:
                self.finalizer.finalize(self.view, version=1, reason="fixture release")
                token = self.controller.records["R1"].backend_job_id
                state_path = self.root / "dispatch" / f"{token}.state.json"
                deadline = time.time() + 2
                while not state_path.exists() and time.time() < deadline:
                    time.sleep(.01)
                state = json.loads(state_path.read_text())
                state.update(state="completed", returncode=0, finished_at=time.time(),
                             artifact={"outcome": "COMPLETE"})
                state_path.write_text(json.dumps(state))
                self.controller.tick()
        except RuntimeError as exc:
            assert self.crash_after in str(exc)

    def restart_and_reconcile(self):
        self.controller.failpoint = lambda *_args: None
        # Reconstruct from both durable controller and ownership projections.
        self.controller = PortfolioController(
            portfolio=self.portfolio, campaign_specs=self.controller.specs,
            capacity={"cpus": 3, "ram_gb": 8}, backend=self.backend,
            state_path=self.root / "controller.json", coordinator=self.coordinator,
            lease_factory=_lease, completion_verifier=self.completion,
        )
        try:
            self.controller.tick()
        except Exception:
            pass
        if any(record.state == "running" for record in self.controller.records.values()):
            self.controller.stop(mode="force")

    def live_process_group_count(self, campaign_id):
        return sum(1 for process in self.backend._processes.values() if process.poll() is None
                   and campaign_id in self.controller.records)

    def trial_count(self, campaign_id, *, idea_id):
        return len([row for row in self.registry.rows("runs")
                    if row["experiment_id"] == campaign_id and row["idea_id"] == idea_id])

    def alias_count(self, campaign_id, *, alias):
        return len([row for row in self.registry.rows("aliases")
                    if row["model_id"] == campaign_id and row["alias"] == alias])


def restart_scenario(tmp_path, *, crash_after):
    return _RestartScenario(tmp_path, crash_after=crash_after)
