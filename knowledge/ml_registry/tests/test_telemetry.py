from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from knowledge.ml_registry.cli import main
from knowledge.ml_registry.telemetry import publish_campaign_telemetry
from knowledge.ml_registry.verdict import LedgerRow
from knowledge.ml_registry.write_path import Fact, RegistrySpace


def _fixture_campaign() -> tuple[RegistrySpace, dict[str, LedgerRow]]:
    model_id = "model-telemetry"
    space = RegistrySpace(
        facts={
            model_id: Fact(
                id=model_id,
                category="model",
                meta={
                    "metric": "validation_loss",
                    "direction": "minimize",
                    "baseline": "arm-5",
                    "baseline_start": "baseline",
                },
            )
        }
    )
    ledger = {"baseline": LedgerRow(1.0, 100.0, 0.0)}
    values = (0.97, 1.03, 1.01, 1.06, 0.92)
    for number, value in enumerate(values, start=1):
        idea_id = f"idea-{number}"
        trial_id = f"trial-{number}"
        commit = f"arm-{number}"
        adopted = number == 5
        reason = "" if adopted else f"axis {number} did not clear the floor"
        space.facts[idea_id] = Fact(
            id=idea_id,
            category="idea",
            meta={
                "model_id": model_id,
                "axis": f"axis-{number}",
                "description": f"try axis {number}",
                "status": "adopted" if adopted else "rejected",
                "rejection_reason": reason,
                "hyperparameters": {"learning_rate": number / 1000},
                "reproduction_instructions": f"uv run train --arm {number}",
            },
        )
        space.facts[trial_id] = Fact(
            id=trial_id,
            category="trial",
            derived_from=(idea_id,),
            meta={
                "model_id": model_id,
                "idea_id": idea_id,
                "commit": commit,
                "status": "succeeded" if adopted else "failed",
                "verdict": "adopted" if adopted else "rejected",
                "baseline_commit": "baseline",
                "training_diagnostics": {
                    "epochs": 10 + number,
                    "final_train_loss": round(value - 0.05, 3),
                },
                "diff_summary": f"changed axis-{number} implementation",
                "diff_blob": f"diff --git a/model.py b/model.py\n+axis_{number} = True\n",
            },
        )
        ledger[commit] = LedgerRow(value, 100.0 - number, float(number * 3))
    return space, ledger


def _trackio_rows(root: Path) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    database = next(root.glob("*.db"))
    with sqlite3.connect(database) as connection:
        runs = [json.loads(row[0]) for row in connection.execute("SELECT config FROM configs")]
        logs = [json.loads(row[0]) for row in connection.execute("SELECT metrics FROM metrics")]
    return runs, logs


def test_five_arm_fixture_renders_all_records_and_survives_campaign_state_deletion(
    tmp_path: Path,
) -> None:
    campaign_state = tmp_path / "campaign_state"
    campaign_state.mkdir()
    telemetry_root = tmp_path / "durable-telemetry"
    space, ledger = _fixture_campaign()
    space.save(campaign_state / "registry.json")

    receipt = publish_campaign_telemetry(
        space,
        "model-telemetry",
        ledger,
        store_root=telemetry_root,
        project="fixture-campaign",
    )

    shutil.rmtree(campaign_state)
    runs, logs = _trackio_rows(telemetry_root)
    record_types = {str(run["record_type"]) for run in runs}
    assert record_types == {"champion", "experiment", "dead-end"}
    assert sum(run["record_type"] == "experiment" for run in runs) == 5
    assert sum(run["record_type"] == "dead-end" for run in runs) == 4

    champion = next(run for run in runs if run["record_type"] == "champion")
    assert champion["hyperparameters"] == {"learning_rate": 0.005}
    assert champion["reproduction_instructions"] == "uv run train --arm 5"

    experiments = [entry for entry in logs if entry.get("record_type") == "experiment"]
    assert len(experiments) == 5
    assert all("metric_delta" in entry and "training_diagnostics" in entry for entry in experiments)

    dead_ends = [entry for entry in logs if entry.get("record_type") == "dead-end"]
    assert len(dead_ends) == 4
    assert all(
        entry.get("tested_axis")
        and entry.get("direction") == "minimize"
        and entry.get("performance_change") is not None
        and entry.get("rejection_reason")
        and entry.get("diff_summary")
        and entry.get("rejected_arm_diff")
        for entry in dead_ends
    )
    assert receipt.experiment_count == 5
    assert receipt.dead_end_count == 4
    assert receipt.database.exists()


def test_campaign_telemetry_cli_wires_the_registry_and_external_ledger(tmp_path: Path) -> None:
    space, ledger = _fixture_campaign()
    space_file = tmp_path / "campaign_state" / "registry.json"
    space_file.parent.mkdir()
    space.save(space_file)
    ledger_file = tmp_path / "results.tsv"
    ledger_file.write_text(
        "commit\tmetric_value\tthroughput\tdiff_lines\tstatus\n"
        + "".join(
            f"{commit}\t{row.value}\t{row.throughput}\t{row.diff_lines}\t{row.status}\n"
            for commit, row in ledger.items()
        )
    )
    store_root = tmp_path / "durable-telemetry"

    assert main([
        "campaign-telemetry",
        "--space-file", str(space_file),
        "--model-id", "model-telemetry",
        "--ledger", str(ledger_file),
        "--store-root", str(store_root),
        "--project", "fixture-cli",
    ]) == 0

    runs, _ = _trackio_rows(store_root)
    assert sum(run["record_type"] == "experiment" for run in runs) == 5
