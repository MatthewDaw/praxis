"""Disposable, domain-neutral proof of the standard-registry campaign lifecycle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Any

from knowledge.ml_registry.domain import CampaignBinding
from knowledge.ml_registry.services import build_campaign_view
from knowledge.ml_registry.services.registry_adjudication import adjudicate_against_champion
from knowledge.ml_registry.services.registry_aliases import adopt_run_and_promote
from knowledge.ml_registry.services.registry_finalize import RegistryFinalizer
from knowledge.ml_registry.services.registry_runs import complete_run
from knowledge.ml_registry.storage import Registry
from knowledge.ml_registry.write_path import RegistrySpace


REPO_ROOT = Path(__file__).resolve().parents[3]


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _metrics(value: float) -> dict[str, object]:
    return {
        "metric": value,
        "validity": "valid",
        "throughput": 10.0,
        "throughput_unit": "samples_per_second",
        "memory_gb": 0.0,
        "cpu_time": 0.0,
        "load": {"start_1m": 0.0, "end_1m": 0.0},
    }


def _create_and_complete_run(
    registry: Registry,
    *,
    repo: Path,
    sha: str,
    run_id: str,
    idea_id: str,
    metric: float,
) -> str:
    registry.create_run(
        run_id=run_id,
        experiment_id="fixture-experiment",
        idea_id=idea_id,
        stage="representation",
        family="fixture-family",
        params={"candidate": run_id},
        metrics={},
        code_ref={
            "schema_version": 1,
            "repo": str(repo),
            "sha": sha,
            "base_sha": sha,
            "diff_hash": "0" * 64,
            "diff_lines": 0,
        },
        device_fingerprint="cpu:fixture",
        status="running",
        verdict=None,
        started_at=1.0,
        finished_at=None,
        claim_owner="fixture",
        heartbeat_at=1.0,
    )
    complete_run(registry, run_id=run_id, metrics=_metrics(metric))
    return registry.create_artifact(
        run_id=run_id,
        kind="checkpoint",
        content=f"fixture-bytes:{run_id}".encode(),
        schema_version="1",
    )


def run_fixture(registry_root: Path, *, repo: Path = REPO_ROOT) -> dict[str, Any]:
    """Create only disposable state and return evidence for the complete lifecycle."""
    sha = _head(repo)
    registry = Registry(registry_root, clock=lambda: 10.0)
    space = RegistrySpace()
    model_fact = space.insert("model", {"metric": "fixture_score"})
    baseline_idea = space.insert(
        "idea",
        {"model_id": model_fact, "id": "baseline", "stage": "representation", "depends_on": []},
    )
    candidate_idea = space.insert(
        "idea",
        {
            "model_id": model_fact,
            "id": "candidate",
            "stage": "representation",
            "depends_on": [baseline_idea],
        },
    )
    registry.create_experiment(
        experiment_id="fixture-experiment",
        spec_digest="f" * 64,
        stages=["representation"],
        metric="fixture_score",
        direction="maximize",
        win_condition={"metric_at_least": 0.7},
        noise_floor=0.01,
        baseline_throughput=9.0,
    )
    registry.register_model(
        model_id="fixture-model",
        family="fixture-family",
        sport_scope="shared",
        axis="fixture-axis",
        protocol="FixtureProtocol",
        extends=None,
    )

    baseline_artifact = _create_and_complete_run(
        registry,
        repo=repo,
        sha=sha,
        run_id="baseline-run",
        idea_id=baseline_idea,
        metric=0.5,
    )
    adopt_run_and_promote(
        registry,
        run_id="baseline-run",
        model_id="fixture-model",
        reason="fixture bootstrap baseline",
        model_version={
            "version": 1,
            "artifact_id": baseline_artifact,
            "checksum": baseline_artifact,
            "family_version": "fixture-family@1",
            "code_sha": sha,
            "preprocessing_hash": "fixture-preprocessing-v1",
            "calibration": {},
            "thresholds": {},
            "compat_result": {"head_sha": sha, "passed": True, "at": 2.0},
            "status": "active",
        },
    )

    candidate_artifact = _create_and_complete_run(
        registry,
        repo=repo,
        sha=sha,
        run_id="candidate-run",
        idea_id=candidate_idea,
        metric=0.75,
    )
    verdict = adjudicate_against_champion(
        registry,
        run_id="candidate-run",
        model_id="fixture-model",
        reason="fixture external comparison",
        promotion={
            "version": 2,
            "artifact_id": candidate_artifact,
            "checksum": candidate_artifact,
            "family_version": "fixture-family@1",
            "code_sha": sha,
            "preprocessing_hash": "fixture-preprocessing-v1",
            "calibration": {},
            "thresholds": {},
            "compat_result": {"head_sha": sha, "passed": True, "at": 3.0},
            "status": "active",
        },
    )
    view = build_campaign_view(
        space,
        registry,
        CampaignBinding("fixture-experiment", "fixture-model", model_fact),
    )
    finalized = RegistryFinalizer(
        registry,
        min_measured=1,
        compatibility_loader=lambda _version, path, _head_sha: (
            path.read_bytes() == b"fixture-bytes:candidate-run"
        ),
    ).finalize(view, version=2, reason="fixture compatibility verified")
    aliases = {row["alias"]: row["version"] for row in registry.rows("aliases")}
    return {
        "registry_root": str(registry_root),
        "experiment_id": "fixture-experiment",
        "run_id": "candidate-run",
        "verdict": verdict,
        "model_id": finalized.model_version.model_id,
        "model_version": finalized.model_version.version,
        "aliases": aliases,
        "table_names": registry.table_names(),
        "event_types": [event.event_type for event in registry.list_events()],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the disposable standard-registry campaign lifecycle fixture."
    )
    parser.add_argument("--registry-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_fixture(args.registry_root), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
