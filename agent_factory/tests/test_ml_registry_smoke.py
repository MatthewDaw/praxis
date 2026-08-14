"""Smoke test for the af-ml-research registry entrypoint (R1/R2, check plan-2f4be5275cf7).

Runs ``python -m knowledge.ml_registry.cli`` as a real subprocess -- not an import -- so a
registry ticket cannot go green on code that has no runnable entrypoint. Exercises the R1
acceptance condition end-to-end through the CLI: a well-formed fact per category is
accepted, a fact missing a required key is rejected naming it, a worker-sourced mutation
of a protected model field is refused naming it, and a baseline move from anywhere other
than adjudication is refused. Also exercises R2's write API: registering a model, an idea
and a trial through the CLI (each call a separate subprocess, persisted via
``--space-file``), a readback returning all three, the trial's ``derived_from`` edge, the
idea origin enum, the per-model discovered-idea budget, and the two trial refusals
(unregistered idea, commit missing from the external ledger).
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


MODEL_META = {
    "metric": "val_bpb",
    "direction": "minimize",
    "win_condition": "beats baseline by noise_floor",
    "baseline": "commit-abc123",
    "noise_floor": 0.01,
    "baseline_throughput": 1200,
    "diff_size_limit": 800,
    "max_discovered_ideas": 1,
}


def _register(command: str, space_file: Path, meta: dict, *, ledger: Path | None = None) -> str:
    args = [command, "--space-file", str(space_file), "--meta-json", json.dumps(meta)]
    if ledger is not None:
        args += ["--ledger", str(ledger)]
    result = _run_cli(*args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().rsplit(" ", 1)[-1]


def test_cli_register_readback_round_trip_across_separate_processes(tmp_path):
    """R2 acceptance: register a model, an idea and a trial through the API (each a
    separate subprocess) and read all three back, with the trial's derived_from edge
    naming the idea's fact id."""
    space_file = tmp_path / "space.json"
    ledger = tmp_path / "results.tsv"
    ledger.write_text("commit\tval_bpb\tmemory_gb\tstatus\tdescription\ndeadbeef\t1.0\t2.0\tok\tbaseline\n")

    model_id = _register("register-model", space_file, MODEL_META)
    idea_id = _register(
        "register-idea",
        space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "try RoPE"},
    )
    trial_id = _register(
        "register-trial",
        space_file,
        {"model_id": model_id, "idea_id": idea_id, "commit": "deadbeef", "status": "running"},
        ledger=ledger,
    )

    result = _run_cli("readback", "--space-file", str(space_file))
    assert result.returncode == 0, result.stderr
    facts = json.loads(result.stdout)
    facts_by_id = {f["id"]: f for f in facts}
    assert set(facts_by_id) == {model_id, idea_id, trial_id}
    assert facts_by_id[trial_id]["derivedFrom"] == [idea_id]


def test_cli_refuses_a_discovered_idea_beyond_the_model_budget_naming_it(tmp_path):
    space_file = tmp_path / "space.json"
    model_id = _register("register-model", space_file, MODEL_META)  # max_discovered_ideas=1
    _register(
        "register-idea",
        space_file,
        {"model_id": model_id, "origin": "discovered", "axis": "architecture", "description": "d1"},
    )
    result = _run_cli(
        "register-idea",
        "--space-file",
        str(space_file),
        "--meta-json",
        json.dumps(
            {"model_id": model_id, "origin": "discovered", "axis": "architecture", "description": "d2"}
        ),
    )
    assert result.returncode == 1
    assert "max_discovered_ideas" in result.stdout + result.stderr


def test_cli_refuses_a_trial_for_an_unregistered_idea(tmp_path):
    space_file = tmp_path / "space.json"
    ledger = tmp_path / "results.tsv"
    ledger.write_text("commit\tval_bpb\tmemory_gb\tstatus\tdescription\ndeadbeef\t1.0\t2.0\tok\tbaseline\n")
    model_id = _register("register-model", space_file, MODEL_META)
    result = _run_cli(
        "register-trial",
        "--space-file",
        str(space_file),
        "--meta-json",
        json.dumps({"model_id": model_id, "idea_id": "idea-nope", "commit": "deadbeef", "status": "running"}),
        "--ledger",
        str(ledger),
    )
    assert result.returncode == 1
    assert "idea_id" in result.stdout + result.stderr


def test_cli_refuses_a_trial_whose_commit_is_missing_from_the_ledger(tmp_path):
    space_file = tmp_path / "space.json"
    ledger = tmp_path / "results.tsv"
    ledger.write_text("commit\tval_bpb\tmemory_gb\tstatus\tdescription\ndeadbeef\t1.0\t2.0\tok\tbaseline\n")
    model_id = _register("register-model", space_file, MODEL_META)
    idea_id = _register(
        "register-idea",
        space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "try RoPE"},
    )
    result = _run_cli(
        "register-trial",
        "--space-file",
        str(space_file),
        "--meta-json",
        json.dumps({"model_id": model_id, "idea_id": idea_id, "commit": "not-in-ledger", "status": "running"}),
        "--ledger",
        str(ledger),
    )
    assert result.returncode == 1
    assert "commit" in result.stdout + result.stderr


def test_cli_resolve_citation_records_basis_and_title_on_the_idea(tmp_path: Path) -> None:
    """R7 acceptance, CLI-driven: a resolving arXiv reference lands basis=external with
    the resolved title recorded on the idea, read back through a separate subprocess."""
    space_file = tmp_path / "space.json"
    model_id = _register("register-model", space_file, MODEL_META)
    idea_id = _register(
        "register-idea",
        space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "try RoPE"},
    )
    result = _run_cli(
        "resolve-citation",
        "--space-file",
        str(space_file),
        "--idea-id",
        idea_id,
        "--reference",
        "2301.12345",
        "--outcome",
        "resolved",
        "--title",
        "Attention Is All You Need",
        "--author",
        "Vaswani",
    )
    assert result.returncode == 0, result.stderr
    readback = json.loads(_run_cli("readback", "--space-file", str(space_file), "--category", "idea").stdout)
    idea = next(f for f in readback if f["id"] == idea_id)
    assert idea["meta"]["basis"] == "external"
    assert idea["meta"]["title"] == "Attention Is All You Need"


def test_cli_resolve_citation_downgrades_to_reasoned_on_the_3rd_consecutive_unreachable_attempt(tmp_path: Path) -> None:
    space_file = tmp_path / "space.json"
    model_id = _register("register-model", space_file, MODEL_META)
    idea_id = _register(
        "register-idea",
        space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "try RoPE"},
    )
    args = [
        "resolve-citation",
        "--space-file",
        str(space_file),
        "--idea-id",
        idea_id,
        "--reference",
        "2301.12345",
        "--outcome",
        "unreachable",
    ]
    for _ in range(3):
        result = _run_cli(*args)
        assert result.returncode == 0, result.stderr
    readback = json.loads(_run_cli("readback", "--space-file", str(space_file), "--category", "idea").stdout)
    idea = next(f for f in readback if f["id"] == idea_id)
    assert idea["meta"]["basis"] == "reasoned"
    assert idea["meta"]["unreachable_streak"] == 0


def test_cli_resolve_citation_refuses_an_unregistered_idea_naming_it(tmp_path: Path) -> None:
    space_file = tmp_path / "space.json"
    result = _run_cli(
        "resolve-citation",
        "--space-file",
        str(space_file),
        "--idea-id",
        "idea-nope",
        "--reference",
        "2301.12345",
        "--outcome",
        "resolved",
    )
    assert result.returncode == 1
    assert "idea_id" in result.stdout + result.stderr
