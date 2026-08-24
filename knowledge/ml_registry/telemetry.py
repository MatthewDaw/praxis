"""Durable, local Trackio projections for one ML research campaign."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock
from types import ModuleType
from typing import Iterator, Mapping

from knowledge.ml_registry.schema import IDEA, MODEL, TRIAL
from knowledge.ml_registry.verdict import LedgerRow
from knowledge.ml_registry.write_path import Fact, RegistrySpace


_TRACKIO_ENV = ("TRACKIO_SPACE_ID", "TRACKIO_SERVER_URL", "TRACKIO_BUCKET_ID", "TRACKIO_DATASET_ID")
_TRACKIO_LOCK = RLock()


@dataclass(frozen=True)
class TelemetryReceipt:
    project: str
    database: Path
    experiment_count: int
    dead_end_count: int


def _fact(space: RegistrySpace, fact_id: str, category: str) -> Fact:
    fact = space.get(fact_id)
    if fact is None or fact.category != category:
        raise ValueError(f"{category} {fact_id!r} was never registered")
    return fact


def _baseline_commit(trial: Fact, model: Fact) -> str:
    for value in (
        trial.meta.get("baseline_commit"),
        trial.meta.get("base_commit"),
        model.meta.get("baseline_start"),
    ):
        if value not in (None, ""):
            return str(value)
    raise ValueError(f"trial {trial.id!r} does not record the baseline commit it ran against")


def _metric_change(direction: str, baseline: float, candidate: float) -> float:
    if direction == "minimize":
        return baseline - candidate
    if direction == "maximize":
        return candidate - baseline
    raise ValueError(f"model direction must be 'minimize' or 'maximize', got {direction!r}")


@contextmanager
def _local_trackio(store_root: Path) -> Iterator[ModuleType]:
    """Bind Trackio's process-global local store for the duration of one projection."""
    with _TRACKIO_LOCK:
        store_root.mkdir(parents=True, exist_ok=True)
        old_env = {key: os.environ.pop(key, None) for key in _TRACKIO_ENV}
        old_dir = os.environ.get("TRACKIO_DIR")
        os.environ["TRACKIO_DIR"] = str(store_root)
        import trackio
        from trackio import sqlite_storage

        previous = sqlite_storage.TRACKIO_DIR
        sqlite_storage.TRACKIO_DIR = store_root
        try:
            yield trackio
        finally:
            sqlite_storage.TRACKIO_DIR = previous
            if old_dir is None:
                os.environ.pop("TRACKIO_DIR", None)
            else:
                os.environ["TRACKIO_DIR"] = old_dir
            for key, value in old_env.items():
                if value is not None:
                    os.environ[key] = value


def _config(idea: Fact, trial: Fact, *, record_type: str) -> dict[str, object]:
    return {
        "record_type": record_type,
        "trial_id": trial.id,
        "idea_id": idea.id,
        "commit": trial.meta.get("commit"),
        "tested_axis": idea.meta.get("axis"),
        "hyperparameters": trial.meta.get("hyperparameters") or idea.meta.get("hyperparameters") or {},
        "reproduction_instructions": (
            trial.meta.get("reproduction_instructions")
            or idea.meta.get("reproduction_instructions")
            or ""
        ),
    }


def _publish_run(
    trackio: ModuleType,
    *,
    project: str,
    name: str,
    group: str,
    config: dict[str, object],
    metrics: dict[str, object],
) -> None:
    trackio.init(
        project=project,
        name=name,
        group=group,
        config=config,
        embed=False,
        auto_log_gpu=False,
        auto_log_cpu=False,
    )
    trackio.log({"record_type": config["record_type"], **metrics})
    trackio.finish()


def _verify_database(database: Path, *, experiments: int, dead_ends: int) -> None:
    with sqlite3.connect(database) as connection:
        configs = [json.loads(row[0]) for row in connection.execute("SELECT config FROM configs")]
        logs = [json.loads(row[0]) for row in connection.execute("SELECT metrics FROM metrics")]
    expected_runs = {"experiment": experiments, "champion": 1, "dead-end": dead_ends}
    for label, rows in (("run", configs), ("log", logs)):
        actual = {
            kind: sum(row.get("record_type") == kind for row in rows)
            for kind in expected_runs
        }
        if actual != expected_runs:
            raise RuntimeError(
                f"Trackio {label} readback mismatch: expected {expected_runs}, got {actual}"
            )


def publish_campaign_telemetry(
    space: RegistrySpace,
    model_id: str,
    ledger_rows: Mapping[str, LedgerRow],
    *,
    store_root: Path,
    project: str,
) -> TelemetryReceipt:
    """Project a completed campaign into the three records shown by ``trackio show``.

    ``store_root`` must be outside disposable ``campaign_state``. Trackio stores the run configs,
    reports, metrics, and rejected-arm diffs in its own SQLite database there.
    """
    model = _fact(space, model_id, MODEL)
    direction = str(model.meta.get("direction") or "")
    trials = sorted(
        (fact for fact in space.list_facts(TRIAL) if fact.meta.get("model_id") == model_id),
        key=lambda fact: fact.id,
    )
    if len(trials) < 5:
        raise ValueError("campaign telemetry requires at least five arms")

    records: list[tuple[Fact, Fact, LedgerRow, float]] = []
    for trial in trials:
        idea = _fact(space, str(trial.meta.get("idea_id") or ""), IDEA)
        commit = str(trial.meta.get("commit") or "")
        row = ledger_rows.get(commit)
        baseline_commit = _baseline_commit(trial, model)
        baseline = ledger_rows.get(baseline_commit)
        if row is None or baseline is None:
            raise ValueError(
                f"trial {trial.id!r} needs ledger rows for arm {commit!r} and baseline {baseline_commit!r}"
            )
        if not trial.meta.get("training_diagnostics"):
            raise ValueError(f"trial {trial.id!r} has no training diagnostics")
        records.append((idea, trial, row, _metric_change(direction, baseline.value, row.value)))

    champion_records = [record for record in records if record[1].meta.get("verdict") == "adopted"]
    if not champion_records:
        raise ValueError("campaign has no adopted arm to render as champion")
    champion = champion_records[-1]
    dead_ends = [record for record in records if record[1].meta.get("verdict") == "rejected"]

    with _local_trackio(store_root) as trackio:
        for idea, trial, row, change in records:
            _publish_run(
                trackio,
                project=project,
                name=f"experiment-{trial.id}",
                group="experiment-log",
                config=_config(idea, trial, record_type="experiment"),
                metrics={
                    "metric_value": row.value,
                    "metric_delta": change,
                    "throughput": row.throughput,
                    "diff_lines": row.diff_lines,
                    "verdict": trial.meta.get("verdict"),
                    "training_diagnostics": trackio.Markdown(
                        "```json\n"
                        + json.dumps(trial.meta["training_diagnostics"], indent=2, sort_keys=True)
                        + "\n```"
                    ),
                },
            )

        idea, trial, row, change = champion
        champion_config = _config(idea, trial, record_type="champion")
        if not champion_config["reproduction_instructions"]:
            raise ValueError(f"champion {trial.id!r} has no reproduction instructions")
        _publish_run(
            trackio,
            project=project,
            name="champion",
            group="champion",
            config=champion_config,
            metrics={
                "metric_value": row.value,
                "metric_delta": change,
                "champion_record": trackio.Markdown(
                    f"# Champion\n\nCommit: `{trial.meta['commit']}`\n\n"
                    f"Reproduce: `{champion_config['reproduction_instructions']}`\n\n"
                    f"Hyperparameters:\n```json\n"
                    f"{json.dumps(champion_config['hyperparameters'], indent=2, sort_keys=True)}\n```"
                ),
            },
        )

        for idea, trial, row, change in dead_ends:
            reason = str(idea.meta.get("rejection_reason") or trial.meta.get("rejection_reason") or "")
            summary = str(trial.meta.get("diff_summary") or "")
            diff_blob = str(trial.meta.get("diff_blob") or "")
            if not reason or not summary or not diff_blob:
                raise ValueError(
                    f"rejected trial {trial.id!r} needs a rejection reason, diff summary, and diff blob"
                )
            _publish_run(
                trackio,
                project=project,
                name=f"dead-end-{trial.id}",
                group="dead-end-registry",
                config=_config(idea, trial, record_type="dead-end"),
                metrics={
                    "tested_axis": idea.meta.get("axis"),
                    "direction": direction,
                    "performance_change": change,
                    "rejection_reason": reason,
                    "diff_summary": summary,
                    "rejected_arm_diff": trackio.Markdown(f"```diff\n{diff_blob}\n```"),
                },
            )

        from trackio.sqlite_storage import SQLiteStorage

        database = SQLiteStorage.get_project_db_path(project)
        _verify_database(database, experiments=len(records), dead_ends=len(dead_ends))

    return TelemetryReceipt(
        project=project,
        database=database,
        experiment_count=len(records),
        dead_end_count=len(dead_ends),
    )
