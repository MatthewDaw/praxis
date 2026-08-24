"""Canonical experiment → run → artifact → adjudication → finalize campaign path."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from knowledge.ml_registry.cli import main
from knowledge.ml_registry.services.registry_adjudication import (
    adjudicate_against_champion,
)
from knowledge.ml_registry.services.registry_aliases import adopt_run_and_promote
from knowledge.ml_registry.services.registry_finalize import RegistryFinalizeService
from knowledge.ml_registry.storage import Registry
from knowledge.ml_registry.write_path import RegistrySpace

REPO = Path(__file__).resolve().parents[3]
SHA = subprocess.run(
    ["git", "-C", str(REPO), "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()


def _run_json(run_id, idea_id):
    return {
        "run_id": run_id,
        "experiment_id": "stroke",
        "idea_id": idea_id,
        "stage": "representation",
        "family": "gru",
        "params": {},
        "metrics": {},
        "code_ref": {
            "schema_version": 1,
            "repo": str(REPO),
            "sha": SHA,
            "base_sha": SHA,
            "diff_hash": "d" * 64,
            "diff_lines": 1,
        },
        "device_fingerprint": "cpu",
        "status": "running",
        "verdict": None,
        "started_at": 1,
        "finished_at": None,
        "claim_owner": "trainer",
        "heartbeat_at": 1,
    }


def _metrics(value, throughput):
    return {
        "metric": value,
        "validity": "valid",
        "throughput": throughput,
        "throughput_unit": "sequences_per_second",
        "memory_gb": 1,
        "cpu_time": 2,
        "load": {"start_1m": 0.1, "end_1m": 0.2},
    }


def _campaign(tmp_path: Path, *, throughput_floor: float = 3.211
              ) -> tuple[Path, Path, str, Registry, str]:
    root = tmp_path / "registry"
    space_path = tmp_path / "space.json"
    space = RegistrySpace()
    model_fact = space.insert("model", {"metric": "stroke_macro_f1"})
    ideas = [
        space.insert(
            "idea", {"model_id": model_fact, "id": name, "stage": "representation"}
        )
        for name in ("baseline", "inside", "winner")
    ]
    space.save(space_path)
    experiment = {
        "experiment_id": "stroke",
        "spec_digest": "a" * 64,
        "stages": ["representation"],
        "metric": "stroke_macro_f1",
        "direction": "maximize",
        "win_condition": {"metric_at_least": 0.70},
        "rope": 0.003,
        "baseline_throughput": throughput_floor,
    }
    assert (
        main(
            [
                "create-experiment",
                "--registry-root",
                str(root),
                "--experiment-json",
                json.dumps(experiment),
            ]
        )
        == 0
    )
    model = {
        "model_id": "stroke_clf",
        "family": "gru",
        "sport_scope": "tennis",
        "axis": "a03",
        "protocol": "StrokeClassifier",
        "extends": None,
    }
    assert (
        main(
            [
                "register-model",
                "--registry-root",
                str(root),
                "--model-json",
                json.dumps(model),
            ]
        )
        == 0
    )
    registry = Registry(root)
    for run_id, idea, value, tput in (
        ("baseline", ideas[0], 0.6795, 3.38),
        ("inside", ideas[1], 0.6810, 3.40),
        ("winner", ideas[2], 0.7034, 3.24),
    ):
        assert (
            main(
                [
                    "create-run",
                    "--registry-root",
                    str(root),
                    "--run-json",
                    json.dumps(_run_json(run_id, idea)),
                ]
            )
            == 0
        )
        assert (
            main(
                [
                    "complete-run",
                    "--registry-root",
                    str(root),
                    "--run-id",
                    run_id,
                    "--metrics-json",
                    json.dumps(_metrics(value, tput)),
                ]
            )
            == 0
        )
    artifact = registry.create_artifact(
        run_id="baseline", kind="checkpoint", content=b"base", schema_version="1"
    )
    adopt_run_and_promote(
        registry,
        run_id="baseline",
        model_id="stroke_clf",
        reason="bootstrap",
        model_version={
            "version": 1,
            "artifact_id": artifact,
            "checksum": artifact,
            "family_version": "gru@1",
            "code_sha": SHA,
            "preprocessing_hash": "prep",
            "calibration": {},
            "thresholds": {},
            "compat_result": {"head_sha": SHA, "passed": True, "at": 1},
            "status": "active",
        },
    )
    RegistryFinalizeService(registry).move_production(
        model_id="stroke_clf", version=1, reason="bootstrap production incumbent"
    )
    assert (
        adjudicate_against_champion(
            registry, run_id="inside", model_id="stroke_clf", reason="inside floor"
        )
        == "parked"
    )
    win_artifact = registry.create_artifact(
        run_id="winner", kind="checkpoint", content=b"winner", schema_version="1"
    )
    promotion = {
        "version": 2,
        "artifact_id": win_artifact,
        "checksum": win_artifact,
        "family_version": "gru@1",
        "code_sha": SHA,
        "preprocessing_hash": "prep",
        "calibration": {},
        "thresholds": {},
        "compat_result": {"head_sha": SHA, "passed": True, "at": 2},
        "status": "active",
    }
    verdict = adjudicate_against_champion(
        registry,
        run_id="winner",
        model_id="stroke_clf",
        reason="clears floor",
        promotion=promotion,
    )
    return root, space_path, model_fact, registry, verdict


def test_documented_bootstrap_then_register_then_verdict_path(tmp_path):
    root, space, model_fact, registry, verdict = _campaign(tmp_path)
    assert verdict == "adopted"
    assert (
        main(
            [
                "finalize",
                "--registry-root",
                str(root),
                "--space-file",
                str(space),
                "--experiment-id",
                "stroke",
                "--model-id",
                "stroke_clf",
                "--model-fact-id",
                model_fact,
                "--version",
                "2",
                "--reason",
                "release",
                "--compatibility-command-json",
                '["/usr/bin/true"]',
            ]
        )
        == 0
    )
    assert (
        next(a for a in registry.aliases() if a["alias"] == "production")["version"]
        == 2
    )


def test_void_fraction_zero_adopts_a_structurally_slower_winner(tmp_path):
    _, _, _, registry, verdict = _campaign(tmp_path, throughput_floor=0)
    assert verdict == "adopted"
    assert (
        json.loads(
            next(r for r in registry.list_runs() if r["run_id"] == "winner")["metrics"]
        )["throughput"]
        == 3.24
    )


def test_next_queue_opens_architecture_after_park_and_adopt(tmp_path):
    _, _, _, registry, _ = _campaign(tmp_path)
    assert [
        r["verdict"]
        for r in registry.list_runs()
        if r["run_id"] in {"inside", "winner"}
    ] == ["parked", "adopted"]
