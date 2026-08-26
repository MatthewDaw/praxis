"""Canonical operator surface for the real portfolio controller stack."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import importlib
import json
from pathlib import Path
import sys
import time
from typing import Any, Mapping

from knowledge.ml_registry import Registry
from knowledge.ml_registry.contracts import CampaignLease
from knowledge.ml_registry.contracts import CampaignOutcome, CampaignOutcomeRecord
from knowledge.ml_registry.controller import ContinueCampaign, ExecutorProcessBackend, PortfolioController
from knowledge.ml_registry.domain import CampaignBinding
from knowledge.ml_registry.portfolio import Portfolio
from knowledge.ml_registry.runtime import LeaseIntentCoordinator
from knowledge.ml_registry.scheduler_cli import _campaigns, _capacity
from knowledge.ml_registry.services.campaign_view import build_campaign_view
from knowledge.ml_registry.services.registry_finalize import RegistryFinalizer
from knowledge.ml_registry.services.registry_runs import supersede_run
from knowledge.ml_registry.write_path import RegistrySpace
from knowledge.ml_registry.lifecycle import claim_idea
from knowledge.ml_registry.staging import next_queue


class OperatorConfigError(ValueError):
    pass


class _LazyCompletionVerifier:
    """Build registry views only after a job has had a chance to bootstrap its model."""

    def __init__(self, registry_root: Path, space_path: Path,
                 bindings: Mapping[str, Mapping[str, object]],
                 terminal_outcomes: Mapping[str, frozenset[CampaignOutcome]]) -> None:
        self.registry_root = registry_root
        self.space_path = space_path
        self.bindings = dict(bindings)
        self.terminal_outcomes = dict(terminal_outcomes)

    def __call__(self, campaign_id: str, polled) -> dict[str, object] | ContinueCampaign | None:
        if polled.artifact is None:
            raise ValueError("completion outcome is missing")
        outcome = CampaignOutcomeRecord.from_mapping(polled.artifact)
        # MEASURED is one completed arm, not terminal campaign completion.
        if outcome.outcome is CampaignOutcome.RETRYABLE:
            return ContinueCampaign(outcome.reason)
        allowed = self.terminal_outcomes.get(campaign_id)
        if outcome.outcome is CampaignOutcome.MEASURED and (
            allowed is None or CampaignOutcome.MEASURED not in allowed
        ):
            return ContinueCampaign(outcome.reason)
        if allowed is not None:
            if outcome.outcome not in allowed:
                expected = ", ".join(sorted(item.value for item in allowed))
                raise ValueError(
                    f"campaign {campaign_id!r} returned {outcome.outcome.value}; "
                    f"expected one of {expected}"
                )
            return None
        if outcome.outcome not in {CampaignOutcome.COMPLETE, CampaignOutcome.PROMOTED}:
            raise ValueError("completion outcome is missing")
        try:
            item = self.bindings[campaign_id]
        except KeyError as exc:
            raise ValueError(f"campaign {campaign_id!r} has no finalization binding") from exc
        registry = Registry(self.registry_root)
        space = RegistrySpace.load(self.space_path)
        binding = CampaignBinding(
            str(item["experiment_id"]), str(item["model_id"]), str(item["model_fact_id"]),
        )
        view = build_campaign_view(space, registry, binding)
        finalized = RegistryFinalizer(
            registry, compatibility_loader=_callable(str(item["compatibility_adapter"])),
            min_measured=int(item.get("min_measured", 3)),
        ).verify(view, version=int(item["version"]))
        version = finalized.model_version
        return {
            "artifact_id": str(item["artifact_type"]),
            "model_id": version.model_id,
            "verdict": "adopted",
            "dataset_manifest_hash": version.checksum,
            "split_manifest_hash": version.preprocessing_hash,
            "prediction_manifest_hash": version.checksum,
            "coverage": 1.0,
            "input_artifact_ids": [],
        }


def _load_document(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise OperatorConfigError("operator config must be a JSON object")
    if value.get("schema_version") != 1:
        raise OperatorConfigError("operator config schema_version must be 1")
    return value


def _callable(reference: str):
    try:
        module, name = reference.split(":", 1)
        value = getattr(importlib.import_module(module), name)
    except (ValueError, ImportError, AttributeError) as exc:
        raise OperatorConfigError(f"cannot load compatibility adapter {reference!r}: {exc}") from exc
    if not callable(value):
        raise OperatorConfigError(f"compatibility adapter {reference!r} is not callable")
    return value


class OperatorRuntime:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.resolve()
        self.config = _load_document(self.config_path)
        base = self.config_path.parent

        def resolve(name: str) -> Path:
            raw = self.config.get(name)
            if not isinstance(raw, str) or not raw:
                raise OperatorConfigError(f"{name} must be a non-empty path")
            path = Path(raw)
            return path if path.is_absolute() else (base / path).resolve()

        self.portfolio_path = resolve("portfolio")
        self.campaigns_path = resolve("campaigns")
        self.capacity_path = resolve("capacity")
        self.registry_root = resolve("registry_root")
        self.space_path = resolve("space_file")
        self.runtime_root = resolve("runtime_root")
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.controller_state = self.runtime_root / "controller.json"
        self.ownership_path = self.runtime_root / "ownership.json"
        self.dispatch_root = self.runtime_root / "dispatch"
        # This file is a generated scheduler projection, never persisted back.  The
        # standard Registry remains the authority and completion is re-verified
        # there; controller-local readiness is reconstructed on every invocation.
        portfolio_document = json.loads(self.portfolio_path.read_text())
        self.portfolio = Portfolio._from_document(portfolio_document, path=None)
        campaign_doc = json.loads(self.campaigns_path.read_text())
        self.campaigns = _campaigns(campaign_doc)
        resources, configured_max, _ = _capacity(json.loads(self.capacity_path.read_text()))
        self.capacity = resources
        requested = self.config.get("max_active", 2)
        if isinstance(requested, bool) or not isinstance(requested, int):
            raise OperatorConfigError("max_active must be 1 or 2")
        self.max_active = min(requested, configured_max, 2)
        self.coordinator = LeaseIntentCoordinator(self.ownership_path)
        self.backend = ExecutorProcessBackend(self.dispatch_root, coordinator=self.coordinator)
        self._raw_campaigns = {
            str(item["id"]): item for item in campaign_doc
        } if isinstance(campaign_doc, list) else {
            str(item["id"]): item for item in campaign_doc.get("campaigns", [])
        }
        self.completion = self._completion_verifier()

    def _completion_verifier(self):
        raw = self.config.get("finalization", {})
        if not isinstance(raw, Mapping):
            raise OperatorConfigError("finalization must be an object")
        campaign_ids = {str(item["id"]) for item in self.campaigns}
        terminal_raw = self.config.get("terminal_outcomes", {})
        if not isinstance(terminal_raw, Mapping):
            raise OperatorConfigError("terminal_outcomes must be an object")
        overlap = sorted(set(raw) & set(terminal_raw))
        if overlap:
            raise OperatorConfigError(
                "campaigns cannot declare both finalization and terminal outcomes: "
                + ", ".join(overlap)
            )
        missing = sorted(campaign_ids - set(raw) - set(terminal_raw))
        if missing:
            raise OperatorConfigError(
                "every campaign requires finalization or explicit terminal outcomes: "
                + ", ".join(missing)
            )
        normalized: dict[str, Mapping[str, object]] = {}
        for campaign_id, item in raw.items():
            if not isinstance(item, Mapping):
                raise OperatorConfigError(f"finalization.{campaign_id} must be an object")
            required = {"experiment_id", "model_id", "model_fact_id", "version",
                        "artifact_type", "compatibility_adapter"}
            absent = sorted(required - set(item))
            if absent:
                raise OperatorConfigError(
                    f"finalization.{campaign_id} is missing: " + ", ".join(absent)
                )
            normalized[str(campaign_id)] = item
        terminal: dict[str, frozenset[CampaignOutcome]] = {}
        supported = {
            CampaignOutcome.MEASURED,
            CampaignOutcome.REFUTED,
            CampaignOutcome.ABANDONED,
        }
        for campaign_id, values in terminal_raw.items():
            if not isinstance(values, list) or not values:
                raise OperatorConfigError(
                    f"terminal_outcomes.{campaign_id} must be a non-empty list"
                )
            try:
                outcomes = frozenset(CampaignOutcome(str(value)) for value in values)
            except ValueError as exc:
                raise OperatorConfigError(
                    f"terminal_outcomes.{campaign_id} contains an unknown outcome"
                ) from exc
            if not outcomes <= supported:
                invalid = ", ".join(sorted(item.value for item in outcomes - supported))
                raise OperatorConfigError(
                    f"terminal_outcomes.{campaign_id} cannot bypass finalization with: {invalid}"
                )
            terminal[str(campaign_id)] = outcomes
        return _LazyCompletionVerifier(
            self.registry_root, self.space_path, normalized, terminal,
        )

    def _lease(self, job, token: str) -> CampaignLease:
        raw = self._raw_campaigns[job.campaign_id]
        named = raw.get("lease", {})
        if not isinstance(named, Mapping):
            raise OperatorConfigError(f"campaign {job.campaign_id} lease must be an object")
        now = time.time()
        cpu_threads = int(named.get("cpu_threads", job.resources.cpus))
        return CampaignLease(
            CampaignLease.VERSION, f"lease:{token}", job.campaign_id, token,
            str(named.get("lane", "gpu" if job.resources.gpus else "cpu")),
            str(named.get("device", "cuda:0" if job.resources.gpus else f"cpu:{job.campaign_id}")),
            bool(named.get("exclusive", True)), cpu_threads,
            str(named.get("cotenancy", "forbid")), bool(named.get("throughput_gated", False)),
            str(named.get("state_root", self.runtime_root / "campaigns" / job.campaign_id)),
            str(named.get("checkout", raw.get("working_directory") or f"checkout:{job.campaign_id}")),
            str(named.get("cache_root", self.runtime_root / "cache" / job.campaign_id)),
            str(named.get("ledger_path", f"registry:runs:{job.campaign_id}")),
            now, now + float(named.get("ttl_seconds", 900)),
        )

    def _prepare_arm(self, job, token: str):
        configured = self.config.get("idea_dispatch", {})
        if not isinstance(configured, Mapping):
            raise OperatorConfigError("idea_dispatch must be an object keyed by campaign id")
        if job.campaign_id not in configured:
            return job
        item = self._completion_verifier().bindings.get(job.campaign_id)
        if item is None:
            return job
        binding = CampaignBinding(str(item["experiment_id"]), str(item["model_id"]), str(item["model_fact_id"]))
        owner = f"campaign:{job.campaign_id}:{token}"
        def select(space):
            view = build_campaign_view(space, Registry(self.registry_root), binding)
            answered, adopted = set(), set()
            entries = []
            for idea in view.ideas:
                latest = max(idea.runs, key=lambda run: (run["started_at"], run["run_id"]), default=None)
                if latest and latest["status"] == "succeeded" and latest["verdict"] in {"adopted", "rejected", "parked"}:
                    answered.add(idea.fact_id)
                    if latest["verdict"] == "adopted":
                        adopted.add(idea.fact_id)
                entries.append({"id": idea.fact_id, "stage": idea.stage, "depends_on": list(idea.depends_on)})
            stages = tuple(json.loads(view.experiment["stages"]))
            stage, queue, _blocked = next_queue(entries, answered, adopted, stages)
            if not queue or stage is None:
                raise OperatorConfigError(f"campaign {job.campaign_id!r} has no eligible IDEA to dispatch")
            idea_id = str(queue[0]["id"])
            if not claim_idea(space, idea_id, owner):
                raise OperatorConfigError(f"selected IDEA {idea_id!r} has a live conflicting claim")
            return idea_id, stage
        # Reuse the RegistrySpace bridge mutation lock: selection and claim are one atomic action.
        from knowledge.ml_registry.cli.registry import _load_mutate_save
        idea_id, stage = _load_mutate_save(str(self.space_path), select)
        environment = dict(job.environment)
        environment.update({"AF_ML_IDEA_ID": idea_id, "AF_ML_IDEA_OWNER": owner, "AF_ML_STAGE": str(stage)})
        return replace(job, environment=environment)


    def controller(self) -> PortfolioController:
        def supersede(campaign_id: str, reason: str) -> None:
            registry = Registry(self.registry_root)
            for run in registry.list_runs(experiment_id=campaign_id):
                if run["status"] in {"running", "complete"}:
                    supersede_run(registry, run_id=run["run_id"], reason=reason)

        return PortfolioController(
            portfolio=self.portfolio, campaign_specs=self.campaigns, capacity=self.capacity,
            backend=self.backend, state_path=self.controller_state, max_active=self.max_active,
            retry_backoff_seconds=float(self.config.get("retry_backoff_seconds", 60)),
            coordinator=self.coordinator, lease_factory=self._lease,
            completion_verifier=self.completion, run_superseder=supersede,
            registry=Registry(self.registry_root), job_preparer=self._prepare_arm,
        )

    def status_document(self) -> dict[str, object]:
        status = asdict(self.controller().status())
        registry = Registry(self.registry_root)
        runs = registry.rows("runs")
        by_experiment: dict[str, list[dict[str, object]]] = {}
        for run in runs:
            by_experiment.setdefault(str(run["experiment_id"]), []).append(run)
        for slot in status["slots"]:
            campaign_id = slot["campaign_id"]
            candidates = by_experiment.get(str(campaign_id), [])
            if not candidates:
                continue
            latest = max(candidates, key=lambda run: (float(run["started_at"]), str(run["run_id"])))
            metrics = latest.get("metrics", {})
            if isinstance(metrics, str):
                try:
                    metrics = json.loads(metrics)
                except json.JSONDecodeError:
                    metrics = {}
            slot.update({
                "stage": latest["stage"],
                "idea_id": latest["idea_id"],
                "run_id": latest["run_id"],
                "latest_metric": metrics.get("metric") if isinstance(metrics, Mapping) else None,
                "verdict": latest["verdict"],
            })
        status["aliases"] = [
            {key: row[key] for key in ("model_id", "alias", "version", "set_by", "reason", "at")}
            for row in registry.rows("aliases")
        ]
        return status

    def explain(self, campaign_id: str) -> dict[str, object]:
        try:
            campaign = self.portfolio.campaigns[campaign_id]
            spec = self._raw_campaigns[campaign_id]
        except KeyError as exc:
            raise OperatorConfigError(f"unknown campaign {campaign_id!r}") from exc
        producers = sorted({item.upstream_model_id for item in campaign.dependencies})
        consumers = sorted(
            candidate.id for candidate in self.portfolio.campaigns.values()
            if any(item.upstream_model_id == campaign.model_id for item in candidate.dependencies)
        )
        readiness = self.portfolio.refresh(campaign_id)
        return {
            "campaign": campaign_id,
            "registered_model": campaign.model_id,
            "spec": spec,
            "producers": producers,
            "consumers": consumers,
            "lease": (None if campaign_id not in self.coordinator.leases
                      else self.coordinator.leases[campaign_id].to_mapping()),
            "ready": readiness.activatable,
            "blocker": None if readiness.activatable else "; ".join(readiness.reasons),
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m knowledge.ml_registry.cli.portfolio",
        description="Run and inspect registry experiments with verified model-version aliases",
    )
    parser.add_argument("--config", required=True, help="versioned portfolio operator JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="reconcile then run the ready experiment frontier")
    run.add_argument("--one-shot", action="store_true")
    run.add_argument("--poll-interval", type=float, default=10)
    status = commands.add_parser("status", help="show runs, progress, leases, aliases, and blockers")
    status.add_argument("--campaign")
    stop = commands.add_parser("stop", help="stop portfolio admission and own every process group")
    mode = stop.add_mutually_exclusive_group(required=True)
    mode.add_argument("--drain", action="store_true")
    mode.add_argument("--force", action="store_true")
    resume = commands.add_parser("resume", help="reconcile durable intents and resume scheduling")
    resume.add_argument("--one-shot", action="store_true")
    resume.add_argument("--poll-interval", type=float, default=10)
    explain = commands.add_parser("explain", help="show exact artifact and lease blockers")
    explain.add_argument("campaign")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        runtime = OperatorRuntime(Path(args.config))
        controller = runtime.controller()
        if args.command in {"run", "resume"}:
            result = controller.run(poll_interval=args.poll_interval, one_shot=args.one_shot)
            print(json.dumps(asdict(result), indent=2, sort_keys=True))
            return 1 if result.status == "blocked" else 0
        if args.command == "status":
            if args.campaign:
                print(json.dumps(runtime.explain(args.campaign), indent=2, sort_keys=True))
            else:
                print(json.dumps(runtime.status_document(), indent=2, sort_keys=True))
            return 0
        if args.command == "stop":
            report = controller.stop(mode="force" if args.force else "drain")
            payload = asdict(report)
            payload = {key: sorted(value) for key, value in payload.items()}
            stop_path = runtime.runtime_root / "stop-report.json"
            stop_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0 if not report.orphaned else 1
        if args.command == "explain":
            print(json.dumps(runtime.explain(args.campaign), indent=2, sort_keys=True))
            return 0
        raise AssertionError(args.command)
    except (OperatorConfigError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
