"""Smoke test for the af-ml-research registry entrypoint (R1, check plan-2f4be5275cf7).

Runs ``python -m knowledge.ml_registry.cli`` as a real subprocess -- not an import -- so a
registry ticket cannot go green on code that has no runnable entrypoint. Exercises the R1
acceptance condition end-to-end through the CLI: a well-formed fact per category is
accepted, a fact missing a required key is rejected naming it, a worker-sourced mutation
of a protected model field is refused naming it, and a baseline move from anywhere other
than adjudication is refused.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "knowledge.ml_registry.cli", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_accepts_a_well_formed_model_fact():
    meta = {
        "metric": "val_bpb",
        "direction": "minimize",
        "win_condition": "beats baseline by noise_floor",
        "baseline": "commit-abc123",
        "noise_floor": 0.01,
        "baseline_throughput": 1200,
        "diff_size_limit": 800,
    }
    result = _run_cli("validate-fact", "--category", "model", "--meta-json", json.dumps(meta))
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_cli_rejects_a_fact_missing_a_required_key_naming_it():
    meta = {"model_id": "model-1", "origin": "seeded", "axis": "architecture"}
    result = _run_cli("validate-fact", "--category", "idea", "--meta-json", json.dumps(meta))
    assert result.returncode == 1
    assert "description" in result.stdout + result.stderr


def test_cli_refuses_worker_sourced_mutation_of_a_protected_field_naming_it():
    patch = {"noise_floor": 0.5}
    result = _run_cli(
        "guard-model-mutation", "--patch-json", json.dumps(patch), "--source", "worker"
    )
    assert result.returncode == 1
    assert "noise_floor" in result.stdout + result.stderr


def test_cli_refuses_a_baseline_move_from_anywhere_other_than_adjudication():
    patch = {"baseline": "commit-def456"}
    result = _run_cli("guard-baseline-move", "--patch-json", json.dumps(patch), "--source", "worker")
    assert result.returncode == 1
    assert "baseline" in result.stdout + result.stderr


def test_cli_allows_a_baseline_move_from_adjudication():
    patch = {"baseline": "commit-def456"}
    result = _run_cli(
        "guard-baseline-move", "--patch-json", json.dumps(patch), "--source", "adjudication"
    )
    assert result.returncode == 0, result.stderr
