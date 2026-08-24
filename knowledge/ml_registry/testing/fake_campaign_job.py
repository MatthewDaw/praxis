"""Deterministic no-training campaign job used by cross-repository runtime fixtures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

from knowledge.ml_registry.contracts import CampaignOutcome, CampaignOutcomeRecord, ProductionAliasRef
from knowledge.ml_registry.domain import CampaignBinding, CampaignView, IdeaInventory
from knowledge.ml_registry.services.registry_aliases import adopt_run_and_promote
from knowledge.ml_registry.services.registry_finalize import RegistryFinalizer
from knowledge.ml_registry.services.registry_runs import complete_run
from knowledge.ml_registry.storage import Registry


def compatibility_load(_version, path: Path, _head_sha: str) -> bool:
    """Load the deterministic fixture bytes through the declared public adapter."""
    return path.read_bytes().startswith(b"fixture-model:")


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _view(registry: Registry, campaign_id: str) -> CampaignView:
    run = next(row for row in registry.rows("runs") if row["run_id"] == f"run-{campaign_id}")
    fact = SimpleNamespace(id=run["idea_id"])
    return CampaignView(
        CampaignBinding(campaign_id, campaign_id, f"model-fact-{campaign_id}"),
        next(row for row in registry.rows("experiments") if row["experiment_id"] == campaign_id),
        next(row for row in registry.rows("registered_models") if row["model_id"] == campaign_id),
        SimpleNamespace(id=f"model-fact-{campaign_id}"),
        (IdeaInventory(fact, campaign_id, "representation", (), (run,)),),
    )


def run_fake_campaign(*, registry_root: Path, repo: Path, campaign_id: str,
                      idea_id: str, output: Path, sleep_seconds: float,
                      setup_required: bool) -> None:
    """Publish one real registry run/version through adjudication and finalization."""
    registry = Registry(registry_root)
    if setup_required:
        registry.create_experiment(
            experiment_id=campaign_id, spec_digest="a" * 64, stages=["representation"],
            metric="fixture_score", direction="maximize", win_condition={"metric_at_least": .5},
            rope=.01, baseline_throughput=1,
        )
        registry.register_model(
            model_id=campaign_id, family="fixture", sport_scope="shared", axis="fixture",
            protocol="FixtureModel", extends=None,
        )
    sha = _head(repo)
    started = time.time()
    registry.create_run(
        run_id=f"run-{campaign_id}", experiment_id=campaign_id,
        idea_id=idea_id, stage="representation", family="fixture", params={},
        metrics={}, code_ref={"schema_version": 1, "repo": str(repo), "sha": sha,
        "base_sha": sha, "diff_hash": "d" * 64, "diff_lines": 1},
        device_fingerprint="cpu:fixture", status="running", verdict=None,
        started_at=started, finished_at=None, claim_owner="fixture-controller",
        heartbeat_at=started,
    )
    time.sleep(sleep_seconds)
    complete_run(registry, run_id=f"run-{campaign_id}", metrics={
        "metric": .9, "validity": "valid", "throughput": 1,
        "throughput_unit": "rows_per_second", "memory_gb": 0,
        "cpu_time": sleep_seconds, "load": {"start_1m": 0, "end_1m": 0},
    })
    artifact_id = registry.create_artifact(
        run_id=f"run-{campaign_id}", kind="checkpoint",
        content=f"fixture-model:{campaign_id}".encode(), schema_version="1",
    )
    adopt_run_and_promote(
        registry, run_id=f"run-{campaign_id}", model_id=campaign_id,
        reason="deterministic fixture adoption",
        model_version={"version": 1, "artifact_id": artifact_id, "checksum": artifact_id,
        "family_version": "fixture@1", "code_sha": sha, "preprocessing_hash": "fixture-prep",
        "calibration": {}, "thresholds": {},
        "compat_result": {"head_sha": sha, "passed": True, "at": time.time()},
        "status": "active"},
    )
    finalizer = RegistryFinalizer(
        registry,
        compatibility_loader=lambda _version, path, _head_sha: path.read_bytes()
        == f"fixture-model:{campaign_id}".encode(),
        min_measured=1,
    )
    finalized = finalizer.finalize(_view(registry, campaign_id), version=1,
                                   reason="deterministic fixture finalization")
    outcome = CampaignOutcomeRecord(
        CampaignOutcomeRecord.VERSION, campaign_id, CampaignOutcome.COMPLETE,
        "production alias verified", 1,
        ProductionAliasRef(campaign_id, finalized.production_alias.version),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(outcome.to_mapping(), sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--idea-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sleep", type=float, required=True)
    parser.add_argument("--setup-required", action="store_true")
    args = parser.parse_args(argv)
    run_fake_campaign(
        registry_root=args.registry, repo=args.repo, campaign_id=args.campaign,
        idea_id=args.idea_id,
        output=args.output, sleep_seconds=args.sleep, setup_required=args.setup_required,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
