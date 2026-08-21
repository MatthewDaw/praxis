from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from knowledge.ml_registry import Registry
from knowledge.ml_registry.domain import VALID_RUN_STATUS_VERDICT_PAIRS
from knowledge.ml_registry.services.registry_adjudication import adjudicate_against_champion
from knowledge.ml_registry.services.registry_aliases import adopt_run_and_promote
from knowledge.ml_registry.services.registry_runs import complete_run
from knowledge.ml_registry.storage import RegistryError


REPO = Path(__file__).resolve().parents[3]
SHA = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], check=True,
                     capture_output=True, text=True).stdout.strip()
DIFF = "d" * 64


def metrics(value: float, *, validity: str = "valid", throughput: float = 3.5,
            unit: str = "rows_per_second") -> dict[str, object]:
    return {"metric": value, "validity": validity, "throughput": throughput,
            "throughput_unit": unit, "memory_gb": 1.25, "cpu_time": 8.0,
            "load": {"start_1m": 0.2, "end_1m": 0.4}}


def create_run(registry: Registry, run_id: str, value: float, *, experiment_id: str = "campaign",
               **metric_overrides: object) -> None:
    registry.create_run(
        run_id=run_id, experiment_id=experiment_id, idea_id=f"idea-{run_id}", stage="representation",
        family="linear", params={}, metrics={}, code_ref={"schema_version": 1, "repo": str(REPO),
        "sha": SHA, "base_sha": SHA, "diff_hash": DIFF, "diff_lines": 1},
        device_fingerprint="cpu:test", status="running", verdict=None, started_at=1,
        finished_at=None, claim_owner="trainer", heartbeat_at=1,
    )
    values = metrics(value)
    values.update(metric_overrides)
    complete_run(registry, run_id=run_id, metrics=values)


def registry_with_champion(tmp_path: Path) -> Registry:
    registry = Registry(tmp_path)
    registry.create_experiment(experiment_id="campaign", spec_digest="a" * 64,
        stages=["representation"], metric="f1", direction="maximize",
        win_condition={"metric_at_least": 0.9}, noise_floor=0.01, baseline_throughput=3.3)
    registry.register_model(model_id="model", family="linear", sport_scope="shared", axis="a01",
                            protocol="Detector", extends=None)
    create_run(registry, "baseline", 0.68)
    artifact = registry.create_artifact(run_id="baseline", kind="checkpoint", content=b"base", schema_version="1")
    adopt_run_and_promote(registry, run_id="baseline", model_id="model", reason="bootstrap",
        model_version={"version": 1, "artifact_id": artifact, "checksum": artifact,
        "family_version": "linear@1", "code_sha": SHA, "preprocessing_hash": "prep",
        "calibration": {}, "thresholds": {},
        "compat_result": {"head_sha": SHA, "passed": True, "at": 1}, "status": "active"})
    return registry


def promotion(registry: Registry, run_id: str, version: int = 2) -> dict[str, object]:
    artifact = registry.create_artifact(run_id=run_id, kind="checkpoint", content=b"winner", schema_version="1")
    return {"version": version, "artifact_id": artifact, "checksum": artifact,
            "family_version": "linear@1", "code_sha": SHA, "preprocessing_hash": "prep",
            "calibration": {}, "thresholds": {},
            "compat_result": {"head_sha": SHA, "passed": True, "at": 2}, "status": "active"}


def test_typed_metrics_reject_missing_invalid_and_nonfinite_measurements(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    registry.create_experiment(experiment_id="campaign", spec_digest="a" * 64, stages=["s"], metric="f1",
        direction="maximize", win_condition={}, noise_floor=0.01, baseline_throughput=1)
    registry.create_run(run_id="run", experiment_id="campaign", idea_id="i", stage="s", family="f",
        params={}, metrics={}, code_ref={"schema_version": 1, "repo": str(REPO), "sha": SHA,
        "base_sha": SHA, "diff_hash": DIFF, "diff_lines": 0}, device_fingerprint="cpu", status="running",
        verdict=None, started_at=1, finished_at=None, claim_owner="trainer", heartbeat_at=1)
    with pytest.raises(RegistryError, match="missing=.*cpu_time"):
        complete_run(registry, run_id="run", metrics={"metric": 1})
    bad = metrics(float("nan"))
    with pytest.raises(RegistryError, match="finite"):
        complete_run(registry, run_id="run", metrics=bad)


@pytest.mark.parametrize(("value", "validity", "expected", "status"), [
    (0.70, "valid", "adopted", "succeeded"),
    (0.685, "valid", "parked", "succeeded"),
    (0.66, "valid", "rejected", "succeeded"),
    (0.72, "invalid", "voided", "voided"),
])
def test_registry_adjudicator_owns_verdict_and_compares_current_champion(
    tmp_path: Path, value: float, validity: str, expected: str, status: str,
) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "candidate", value, validity=validity)
    inputs = promotion(registry, "candidate") if expected == "adopted" else None
    assert adjudicate_against_champion(registry, run_id="candidate", model_id="model",
                                      reason="external comparison", promotion=inputs) == expected
    row = next(row for row in registry.rows("runs") if row["run_id"] == "candidate")
    assert row["verdict"] == expected and row["status"] == status
    champion = next(row for row in registry.rows("aliases") if row["alias"] == "champion")
    assert champion["version"] == (2 if expected == "adopted" else 1)


def test_adoption_requires_promotion_inputs_and_units_must_match(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "winner", .72)
    with pytest.raises(RegistryError, match="artifact and compatibility"):
        adjudicate_against_champion(registry, run_id="winner", model_id="model", reason="won")
    assert next(row for row in registry.rows("runs") if row["run_id"] == "winner")["verdict"] is None

    other = registry_with_champion(tmp_path / "other")
    create_run(other, "candidate", .72, throughput_unit="samples_per_second")
    with pytest.raises(RegistryError, match="incomparable"):
        adjudicate_against_champion(other, run_id="candidate", model_id="model", reason="won")


def test_exactly_eight_registry_tables_and_metrics_are_canonical_json(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    assert len(registry.table_names()) == 8
    stored = json.loads(next(row for row in registry.rows("runs") if row["run_id"] == "baseline")["metrics"])
    assert stored == metrics(.68)


def test_run_status_and_scientific_verdict_have_one_exhaustive_pair_matrix() -> None:
    assert VALID_RUN_STATUS_VERDICT_PAIRS == {
        ("running", None), ("complete", None), ("succeeded", "adopted"),
        ("succeeded", "rejected"), ("succeeded", "parked"), ("failed", None),
        ("voided", "voided"), ("superseded", None),
    }


def test_atomic_adoption_retry_is_idempotent_and_semantic_drift_is_refused(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "winner", .72)
    inputs = promotion(registry, "winner")
    assert adjudicate_against_champion(registry, run_id="winner", model_id="model",
                                      reason="won", promotion=inputs) == "adopted"
    count = len(registry.list_events())
    assert adjudicate_against_champion(registry, run_id="winner", model_id="model",
                                      reason="won", promotion=inputs) == "adopted"
    assert len(registry.list_events()) == count
    with pytest.raises(RegistryError, match="full semantic payload"):
        adjudicate_against_champion(registry, run_id="winner", model_id="model",
                                    reason="different", promotion=inputs)


def test_atomic_adoption_recovers_all_projections_after_event_boundary_crash(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    create_run(registry, "winner", .72)
    inputs = promotion(registry, "winner")

    def crash(event):
        if event.event_type == "run_adopted":
            raise RuntimeError("crash after atomic adoption event")

    registry.after_event = crash
    with pytest.raises(RuntimeError, match="atomic adoption event"):
        adjudicate_against_champion(registry, run_id="winner", model_id="model",
                                    reason="won", promotion=inputs)
    assert next(row for row in registry.rows("runs") if row["run_id"] == "winner")["status"] == "complete"
    assert not any(row["run_id"] == "winner" for row in registry.rows("model_versions"))
    assert next(row for row in registry.rows("aliases") if row["alias"] == "champion")["version"] == 1

    recovered = Registry(tmp_path)
    run = next(row for row in recovered.rows("runs") if row["run_id"] == "winner")
    assert (run["status"], run["verdict"]) == ("succeeded", "adopted")
    assert any(row["run_id"] == "winner" for row in recovered.rows("model_versions"))
    assert next(row for row in recovered.rows("aliases") if row["alias"] == "champion")["version"] == 2


def test_champion_baseline_cannot_cross_experiment_boundary(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    registry.create_experiment(experiment_id="other", spec_digest="b" * 64, stages=["representation"],
        metric="f1", direction="maximize", win_condition={}, noise_floor=.01, baseline_throughput=3.3)
    create_run(registry, "other-run", .72, experiment_id="other")
    with pytest.raises(RegistryError, match="different experiment"):
        adjudicate_against_champion(registry, run_id="other-run", model_id="model", reason="crossed")
