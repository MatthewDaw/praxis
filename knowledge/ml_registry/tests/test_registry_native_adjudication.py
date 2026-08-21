from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from knowledge.ml_registry import Registry
from knowledge.ml_registry.domain import VALID_RUN_STATUS_VERDICT_PAIRS
from knowledge.ml_registry.services.registry_adjudication import adjudicate_against_champion
from knowledge.ml_registry.services.registry_aliases import adopt_run_and_promote
from knowledge.ml_registry.services.registry_aliases import record_ratchet_evidence
from knowledge.ml_registry.services.registry_ratchet import consider_rejection, reconcile_registry_space_requeue
from knowledge.ml_registry.services.registry_runs import complete_run
from knowledge.ml_registry.storage import RegistryError
from knowledge.ml_registry.write_path import Fact, RegistrySpace


REPO = Path(__file__).resolve().parents[3]
SHA = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], check=True,
                     capture_output=True, text=True).stdout.strip()
DIFF = "d" * 64
FAIRNESS = {"dataset_digest": "dataset-v1", "split_digest": "split-v1", "seed": 17,
            "harness_digest": "harness-v1", "preprocessing_digest": "preprocess-v1"}


def metrics(value: float, *, validity: str = "valid", throughput: float = 3.5,
            unit: str = "rows_per_second") -> dict[str, object]:
    return {"metric": value, "validity": validity, "throughput": throughput,
            "throughput_unit": unit, "memory_gb": 1.25, "cpu_time": 8.0,
            "load": {"start_1m": 0.2, "end_1m": 0.4}}


def create_run(registry: Registry, run_id: str, value: float, *, experiment_id: str = "campaign",
               idea_id: str | None = None, params: dict[str, object] | None = None,
               **metric_overrides: object) -> None:
    registry.create_run(
        run_id=run_id, experiment_id=experiment_id, idea_id=idea_id or f"idea-{run_id}", stage="representation",
        family="linear", params=params or {}, metrics={}, code_ref={"schema_version": 1, "repo": str(REPO),
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
    artifact = registry.create_artifact(run_id=run_id, kind="checkpoint",
                                        content=f"winner:{run_id}".encode(), schema_version="1")
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


def adopt_version(registry: Registry, run_id: str, value: float, version: int) -> None:
    create_run(registry, run_id, value)
    adjudicate_against_champion(
        registry, run_id=run_id, model_id="model", reason="adoption",
        promotion=promotion(registry, run_id, version),
    )


def paired_rejection(registry: Registry, number: int, *, observed: float, counterfactual: float,
                     active_version: int = 2, parent_version: int = 1,
                     digest: str | None = None, validity: str = "valid") -> None:
    idea_id = f"idea-pair-{number}"
    digest = digest or f"sha256:pair-{number}"
    create_run(registry, f"cf-{number}", counterfactual, idea_id=idea_id,
               params={"evaluated_model_version": parent_version, "intervention_digest": digest,
                       **FAIRNESS},
               validity=validity)
    create_run(registry, f"observed-{number}", observed, idea_id=idea_id,
               params={"evaluated_model_version": active_version, "intervention_digest": digest,
                       "rejected_under_lineage_id": f"model@{active_version}", **FAIRNESS})
    adjudicate_against_champion(
        registry, run_id=f"observed-{number}", model_id="model", reason="paired rejection",
        counterfactual_run_id=f"cf-{number}", intervention_digest=digest,
    )


def test_registry_genuine_win_survives_three_noop_pairs_against_its_parent(tmp_path: Path) -> None:
    """Registry fixture for the old streak ratchet's genuine-win counterexample."""
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "real-win", .80, 2)

    for number in range(3):
        paired_rejection(registry, number, observed=.68, counterfactual=.68)

    champion = next(row for row in registry.rows("aliases") if row["alias"] == "champion")
    assert champion["version"] == 2
    assert not any(event.event_type == "adoption_invalidated" for event in registry.list_events())


def test_registry_ratchet_rolls_back_harm_inherited_by_three_distinct_children(tmp_path: Path) -> None:
    """Registry fixture for the old counterfactual rule's harmful-adoption blind spot."""
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "bad-adoption", .70, 2)

    for number in range(3):
        paired_rejection(registry, number, observed=.50, counterfactual=.68)

    champion = next(row for row in registry.rows("aliases") if row["alias"] == "champion")
    assert (champion["version"], champion["set_by"]) == (1, "ratchet")
    adoption = next(row for row in registry.rows("runs") if row["run_id"] == "bad-adoption")
    assert (adoption["status"], adoption["verdict"]) == ("superseded", None)
    rollback = [event for event in registry.list_events() if event.event_type == "adoption_invalidated"]
    assert len(rollback) == 1
    assert rollback[0].payload["evidence_run_ids"] == ["observed-0", "observed-1", "observed-2"]
    assert rollback[0].payload["requeue_idea_ids"] == ["idea-pair-0", "idea-pair-1", "idea-pair-2"]
    assert len(registry.table_names()) == 8


def test_ratchet_evidence_retry_is_idempotent_and_semantic_drift_is_refused(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "adoption", .70, 2)
    paired_rejection(registry, 0, observed=.50, counterfactual=.68)
    evidence = [event for event in registry.list_events()
                if event.event_type == "ratchet_evidence_recorded"]
    count = len(registry.list_events())
    assert consider_rejection(
        registry, run_id="observed-0", model_id="model",
        counterfactual_run_id="cf-0", intervention_digest="sha256:pair-0",
    ) is False
    assert len(registry.list_events()) == count
    drift = dict(evidence[0].payload)
    drift["evidence_digest"] = "0" * 64
    with pytest.raises(RegistryError, match="retry drifted"):
        record_ratchet_evidence(registry, drift)


def test_retry_of_pair_that_fired_rollback_is_idempotently_true(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "adoption", .70, 2)
    for number in range(3):
        paired_rejection(registry, number, observed=.50, counterfactual=.68)
    count = len(registry.list_events())
    assert consider_rejection(
        registry, run_id="observed-2", model_id="model",
        counterfactual_run_id="cf-2", intervention_digest="sha256:pair-2",
    ) is True
    assert len(registry.list_events()) == count


def test_ratchet_rollback_recovers_atomically_after_event_boundary_crash(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "bad-adoption", .70, 2)
    paired_rejection(registry, 0, observed=.50, counterfactual=.68)
    paired_rejection(registry, 1, observed=.50, counterfactual=.68)

    def crash(event):
        if event.event_type == "adoption_invalidated":
            raise RuntimeError("crash after rollback event")

    registry.after_event = crash
    with pytest.raises(RuntimeError, match="rollback event"):
        paired_rejection(registry, 2, observed=.50, counterfactual=.68)
    assert next(row for row in registry.rows("aliases") if row["alias"] == "champion")["version"] == 2

    recovered = Registry(tmp_path)
    champion = next(row for row in recovered.rows("aliases") if row["alias"] == "champion")
    adoption = next(row for row in recovered.rows("runs") if row["run_id"] == "bad-adoption")
    assert (champion["version"], champion["set_by"]) == (1, "ratchet")
    assert (adoption["status"], adoption["verdict"]) == ("superseded", None)


def test_stacked_adoption_records_current_champion_as_its_direct_parent(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "win-2", .70, 2)
    adopt_version(registry, "win-3", .72, 3)
    edge = next(row for row in registry.rows("lineage")
                if row["child_model_id"] == "model" and row["child_version"] == 3)
    assert (edge["parent_model_id"], edge["parent_version"], edge["kind"]) == (
        "model", 2, "derived_from")


def test_invalidated_version_is_effectively_superseded_and_cannot_be_production(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "adoption", .70, 2)
    for number in range(3):
        paired_rejection(registry, number, observed=.50, counterfactual=.68)
    assert registry.effective_model_version("model", 2)["effective_status"] == "superseded"
    from knowledge.ml_registry.services.registry_finalize import RegistryFinalizeService
    with pytest.raises(ValueError, match="incompatible"):
        RegistryFinalizeService(registry).move_production(
            model_id="model", version=2, reason="invalidated lineage must not ship")


def test_registry_space_requeue_reconciles_exact_lineage_once(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "adoption", .70, 2)
    for number in range(3):
        paired_rejection(registry, number, observed=.50, counterfactual=.68)
    rollback = next(event for event in registry.list_events() if event.event_type == "adoption_invalidated")
    space = RegistrySpace()
    for number in range(3):
        idea_id = f"idea-pair-{number}"
        space.facts[idea_id] = Fact(idea_id, "idea", {
            "model_id": "model", "origin": "seeded", "axis": "a", "description": idea_id,
            "status": "rejected", "rejection_reason": "bad bar",
            "rejected_under_lineage_id": "model@2",
        })
    unrelated = "idea-unrelated"
    space.facts[unrelated] = Fact(unrelated, "idea", {
        "model_id": "model", "origin": "seeded", "axis": "a", "description": unrelated,
        "status": "rejected", "rejected_under_lineage_id": "model@1",
    })
    first = reconcile_registry_space_requeue(registry, space, event_sequence=rollback.sequence)
    second = reconcile_registry_space_requeue(registry, space, event_sequence=rollback.sequence)
    assert first["newly_requeued_idea_ids"] == tuple(f"idea-pair-{i}" for i in range(3))
    assert second["newly_requeued_idea_ids"] == ()
    assert space.get(unrelated).meta["status"] == "rejected"


@pytest.mark.parametrize("fault", ["missing", "unfair", "digest", "wrong_parent"])
def test_missing_unfair_or_mismatched_paired_evidence_cannot_advance_ratchet(
    tmp_path: Path, fault: str,
) -> None:
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "adoption", .70, 2)
    idea_id = "idea-fault"
    digest = "sha256:expected"
    if fault != "missing":
        create_run(registry, "cf", .68, idea_id=idea_id,
                   params={"evaluated_model_version": 9 if fault == "wrong_parent" else 1,
                           "intervention_digest": "sha256:tampered" if fault == "digest" else digest,
                           **FAIRNESS},
                   validity="invalid" if fault == "unfair" else "valid")
    create_run(registry, "observed", .50, idea_id=idea_id,
               params={"evaluated_model_version": 2, "intervention_digest": digest,
                       "rejected_under_lineage_id": "model@2", **FAIRNESS})

    if fault == "missing":
        assert adjudicate_against_champion(
            registry, run_id="observed", model_id="model", reason="ordinary rejection") == "rejected"
    else:
        with pytest.raises(RegistryError, match="unfair|digest|version"):
            adjudicate_against_champion(
                registry, run_id="observed", model_id="model", reason="bad evidence",
                counterfactual_run_id="cf", intervention_digest=digest,
            )
    assert not any(event.event_type == "ratchet_evidence_recorded" for event in registry.list_events())
    assert next(row for row in registry.rows("aliases") if row["alias"] == "champion")["version"] == 2


@pytest.mark.parametrize("field", [
    "dataset_digest", "split_digest", "seed", "harness_digest", "preprocessing_digest",
])
def test_every_explicit_fairness_signature_field_must_match(tmp_path: Path, field: str) -> None:
    registry = registry_with_champion(tmp_path)
    adopt_version(registry, "adoption", .70, 2)
    observed_params = {"evaluated_model_version": 2, "intervention_digest": "sha256:pair",
                       "rejected_under_lineage_id": "model@2", **FAIRNESS}
    paired_params = {"evaluated_model_version": 1, "intervention_digest": "sha256:pair", **FAIRNESS}
    paired_params[field] = "different"
    create_run(registry, "cf", .68, idea_id="idea", params=paired_params)
    create_run(registry, "observed", .50, idea_id="idea", params=observed_params)
    with pytest.raises(RegistryError, match=field):
        adjudicate_against_champion(
            registry, run_id="observed", model_id="model", reason="unfair signature",
            counterfactual_run_id="cf", intervention_digest="sha256:pair",
        )
