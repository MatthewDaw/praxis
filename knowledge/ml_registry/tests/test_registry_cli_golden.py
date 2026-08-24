"""Golden public surface for the pre-P8 semantic CLI cutover."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from knowledge.ml_registry.cli import main


ROOT = Path(__file__).resolve().parents[3]
LIVE_COMMANDS = {
    "create-experiment", "create-run", "complete-run", "create-artifact",
    "register-model", "create-lineage", "adjudicate-run", "registry-status", "finalize",
}
HISTORICAL_COMMANDS = {
    "import-historical-archive", "import-historical-evidence-freeze",
    "import-historical-ledger", "export-runs",
}
IDEA_BRIDGE_COMMANDS = {
    "register-idea", "resolve-citation", "claim-idea", "heartbeat-idea-claim",
    "adopt-idea", "park-idea", "reject-idea", "invalidate-adoption", "reopen-idea",
    "backlog", "rejection-memory", "retriable-ideas", "seed-campaign", "readback",
    "campaign-telemetry",
}


def help_text(*command: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "knowledge.ml_registry.cli", *command, "--help"],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0
    return result.stdout


def test_root_help_is_the_desired_registry_and_idea_bridge_golden() -> None:
    text = help_text()
    for command in LIVE_COMMANDS | HISTORICAL_COMMANDS | IDEA_BRIDGE_COMMANDS:
        assert command in text
    for removed in {
        "bootstrap-campaign", "campaign-complete", "campaign-status", "register-trial",
        "supervise-campaign", "register-model-with-baseline", "retire-harness",
        "record-keep-pushing-marker", "record-out-of-diff-change",
    }:
        assert removed not in text
    normalized = text.lower().replace("-", "")
    for removed_noun in {
        "artifact" + "store", "promotion" + "record", "campaign" + "artifact",
        "convergence" + "_run",
    }:
        assert removed_noun not in normalized


def test_private_pre_cutover_dispatcher_is_absent_and_obsolete_commands_are_unreachable() -> None:
    source = (ROOT / "knowledge/ml_registry/cli/registry.py").read_text()
    assert "def _legacy_main(" not in source
    assert "_legacy_main(" not in source
    for command in ("bootstrap-campaign", "campaign-complete", "supervise-campaign"):
        result = subprocess.run(
            [sys.executable, "-m", "knowledge.ml_registry.cli", command],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        assert result.returncode == 2
        assert "invalid choice" in result.stderr


def test_every_live_registry_command_names_registry_root_authority() -> None:
    for command in LIVE_COMMANDS | HISTORICAL_COMMANDS:
        assert "--registry-root" in help_text(command)
    for command in IDEA_BRIDGE_COMMANDS:
        text = help_text(command)
        assert "--space-file" in text
        assert "--registry-root" not in text


def test_registry_status_reads_canonical_store(tmp_path: Path, capsys) -> None:
    root = tmp_path / "registry"
    experiment = {
        "experiment_id": "fixture", "spec_digest": "d" * 64, "stages": ["model"],
        "metric": "score", "direction": "maximize", "win_condition": {"delta": 0.1},
        "noise_floor": 0.01, "baseline_throughput": 1.0,
    }
    assert main(["create-experiment", "--registry-root", str(root),
                 "--experiment-json", json.dumps(experiment)]) == 0
    capsys.readouterr()
    assert main(["registry-status", "--registry-root", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["experiments"][0]["experiment_id"] == "fixture"
    assert set(payload) == {
        "experiments", "runs", "artifacts", "registered_models", "model_versions", "aliases",
    }


def test_historical_ledger_is_only_an_explicit_import_and_export(tmp_path: Path, capsys) -> None:
    root = tmp_path / "registry"
    source = tmp_path / "sealed.tsv"
    source.write_bytes(
        b"commit\tmetric_value\tmemory_gb\tstatus\tdescription\tthroughput\tdiff_lines\r\n"
        b"abc1234:arm\t.8\t1\tok\tarm\t2\t3\r\n"
    )
    assert main([
        "import-historical-ledger", "--registry-root", str(root), "--input", str(source),
        "--experiment-id", "fixture", "--spec-digest", "d" * 64,
        "--metric", "score", "--direction", "maximize",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {"imported_runs": 1}
    target = tmp_path / "export.tsv"
    assert main(["export-runs", "--registry-root", str(root),
                 "--experiment-id", "fixture", "--output", str(target)]) == 0
    assert target.read_bytes() == source.read_bytes()
