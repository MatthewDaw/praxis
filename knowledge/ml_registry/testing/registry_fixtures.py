"""Thin registry-integrity fixtures built only through canonical writers."""

from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace

from knowledge.ml_registry import Registry
from knowledge.ml_registry.services.registry_aliases import adopt_run_and_promote
from knowledge.ml_registry.services.registry_runs import complete_run


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def registry_invariant_scenario(tmp_path):
    repo = Path(tmp_path) / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Registry Fixture")
    _git(repo, "config", "user.email", "registry-fixture@example.invalid")
    (repo / "model.py").write_text("MODEL = 'fixture'\n")
    _git(repo, "add", "model.py")
    _git(repo, "commit", "-qm", "fixture model")
    sha = _git(repo, "rev-parse", "HEAD")

    registry = Registry(Path(tmp_path) / "registry")
    registry.create_experiment(
        experiment_id="fixture", spec_digest="a" * 64, stages=["representation"],
        metric="f1", direction="maximize", win_condition={"metric_at_least": .5},
        noise_floor=.01, baseline_throughput=1,
    )
    registry.register_model(
        model_id="fixture-model", family="linear", sport_scope="shared", axis="fixture",
        protocol="Fixture", extends=None,
    )
    for version in (1, 2):
        run_id = f"run-{version}"
        registry.create_run(
            run_id=run_id, experiment_id="fixture", idea_id=f"idea-{version}",
            stage="representation", family="linear", params={}, metrics={},
            code_ref={"schema_version": 1, "repo": str(repo), "sha": sha,
                      "base_sha": sha, "diff_hash": "d" * 64, "diff_lines": 1},
            device_fingerprint="cpu:fixture", status="running", verdict=None,
            started_at=float(version), finished_at=None, claim_owner="fixture", heartbeat_at=1,
        )
        complete_run(registry, run_id=run_id, metrics={
            "metric": .7 + version / 100, "validity": "valid", "throughput": 1,
            "throughput_unit": "rows_per_second", "memory_gb": .1, "cpu_time": 1,
            "load": {"start_1m": 0, "end_1m": 0},
        })
        artifact = registry.create_artifact(
            run_id=run_id, kind="checkpoint", content=f"model-{version}".encode(),
            schema_version="1",
        )
        adopt_run_and_promote(
            registry, run_id=run_id, model_id="fixture-model",
            reason=f"fixture adoption {version}",
            model_version={"version": version, "artifact_id": artifact,
                "checksum": artifact, "family_version": "linear@1", "code_sha": sha,
                "preprocessing_hash": "prep", "calibration": {}, "thresholds": {},
                "compat_result": {"head_sha": sha, "passed": True, "at": version},
                "status": "active"},
        )
    return SimpleNamespace(registry=registry, repo=repo)
