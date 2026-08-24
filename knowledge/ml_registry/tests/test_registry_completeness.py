from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from knowledge.ml_registry.domain import CampaignBinding
from knowledge.ml_registry.services import build_campaign_view, campaign_completeness, campaign_coverage
from knowledge.ml_registry.services.registry_aliases import adjudicate_run, adopt_run_and_promote, supersede_run
from knowledge.ml_registry.services.registry_finalize import RegistryFinalizeService
from knowledge.ml_registry.services.registry_runs import complete_run
from knowledge.ml_registry.storage import Registry
from knowledge.ml_registry.write_path import RegistrySpace


REPO = Path(__file__).resolve().parents[3]
SHA = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], check=True,
                     capture_output=True, text=True).stdout.strip()


def _metrics():
    return {"metric": .9, "validity": "valid", "throughput": 2.,
            "throughput_unit": "rows_per_second", "memory_gb": 1., "cpu_time": 2.,
            "load": {"start_1m": .1, "end_1m": .2}}


def _fixture(tmp_path: Path, stages=("representation",)):
    space = RegistrySpace()
    model_fact = space.insert("model", {"metric": "score"})
    registry = Registry(tmp_path, clock=lambda: 10.)
    registry.create_experiment(experiment_id="campaign", spec_digest="d" * 64, stages=list(stages),
                               metric="score", direction="maximize", win_condition={"delta": .1},
                               rope=.01, baseline_throughput=1.)
    registry.register_model(model_id="model", family="f", sport_scope="shared", axis="a",
                            protocol="P", extends=None)
    return space, registry, CampaignBinding("campaign", "model", model_fact)


def _idea(space, binding, name, stage="representation", depends=()):
    return space.insert("idea", {"model_id": binding.model_fact_id, "id": name, "stage": stage,
                                 "depends_on": list(depends)})


def _run(registry, idea, name, *, params=None, verdict="rejected"):
    registry.create_run(run_id=name, experiment_id="campaign", idea_id=idea, stage="representation",
                        family="f", params=params or {}, metrics={}, code_ref={"schema_version": 1,
                        "repo": str(REPO), "sha": SHA, "base_sha": SHA, "diff_hash": "a" * 64,
                        "diff_lines": 1}, device_fingerprint="cpu", status="running", verdict=None,
                        started_at=float(len(registry.rows("runs")) + 1), finished_at=None,
                        claim_owner="w", heartbeat_at=1.)
    if verdict == "superseded":
        supersede_run(registry, run_id=name, reason="retry")
    else:
        complete_run(registry, run_id=name, metrics=_metrics())
        adjudicate_run(registry, run_id=name, verdict=verdict,
                       status="voided" if verdict == "voided" else "succeeded", reason="external")


def _view(space, registry, binding):
    return build_campaign_view(space, registry, binding)


def _force_run_state(registry, run_id, status, verdict):
    with registry._connect("run_superseded") as db:
        db.execute("DROP TRIGGER guard_runs_update")
        db.execute("UPDATE runs SET status=?, verdict=? WHERE run_id=?", (status, verdict, run_id))


@pytest.mark.parametrize(("status", "verdict", "measured", "retry"), [
    ("running", None, 0, False),
    ("complete", None, 0, False),
    ("failed", None, 0, True),
    ("voided", "voided", 0, True),
    ("superseded", None, 0, True),
    ("succeeded", "rejected", 1, False),
])
def test_latest_run_status_and_verdict_drive_coverage(
    tmp_path, status, verdict, measured, retry,
):
    space, registry, binding = _fixture(tmp_path)
    idea = _idea(space, binding, "arm")
    _run(registry, idea, "older")
    registry.create_run(run_id="latest", experiment_id="campaign", idea_id=idea,
                        stage="representation", family="f", params={}, metrics={},
                        code_ref={"schema_version": 1, "repo": str(REPO), "sha": SHA,
                        "base_sha": SHA, "diff_hash": "b" * 64, "diff_lines": 1},
                        device_fingerprint="cpu", status="running", verdict=None, started_at=2.,
                        finished_at=None, claim_owner="w", heartbeat_at=2.)
    _force_run_state(registry, "latest", status, verdict)
    out = campaign_coverage(_view(space, registry, binding), registry, min_measured=1)
    assert out["coverage"][0]["measured"] == measured
    assert ("awaiting_rerun" in {item["kind"] for item in out["blocking"]}) is retry


def test_empty_open_and_thin_stages_have_distinct_blockers(tmp_path):
    space, registry, binding = _fixture(tmp_path, stages=("empty", "open", "thin"))
    _idea(space, binding, "open", stage="open")
    thin = _idea(space, binding, "thin", stage="thin")
    registry.create_run(run_id="thin", experiment_id="campaign", idea_id=thin, stage="thin",
                        family="f", params={}, metrics={}, code_ref={"schema_version": 1,
                        "repo": str(REPO), "sha": SHA, "base_sha": SHA, "diff_hash": "a" * 64,
                        "diff_lines": 1}, device_fingerprint="cpu", status="running", verdict=None,
                        started_at=1., finished_at=None, claim_owner="w", heartbeat_at=1.)
    complete_run(registry, run_id="thin", metrics=_metrics())
    adjudicate_run(registry, run_id="thin", verdict="rejected", status="succeeded", reason="loss")
    out = campaign_coverage(_view(space, registry, binding), registry, min_measured=2)
    assert [(item["kind"], item["stage"]) for item in out["blocking"]] == [
        ("stage_never_authored", "empty"), ("stage_open", "open"), ("stage_thin", "thin")]


@pytest.mark.parametrize("params", [
    {"incumbent_remeasurement": True},
    {"resolved_configuration": "same", "incumbent_configuration": "same"},
])
def test_both_noop_encodings_are_answered_but_not_measured(tmp_path, params):
    space, registry, binding = _fixture(tmp_path)
    idea = _idea(space, binding, "noop")
    _run(registry, idea, "noop", params=params)
    out = campaign_coverage(_view(space, registry, binding), registry, min_measured=1)
    assert out["coverage"][0] == {"stage": "representation", "total": 1, "measured": 0,
                                  "closed": True, "thin": True}
    assert [(item["kind"], item["stage"]) for item in out["blocking"]] == [
        ("stage_thin", "representation")]


def test_latest_retry_and_noop_do_not_populate_or_close_stage(tmp_path):
    space, registry, binding = _fixture(tmp_path)
    idea = _idea(space, binding, "arm")
    _run(registry, idea, "fair")
    _run(registry, idea, "retry", verdict="voided")
    out = campaign_coverage(_view(space, registry, binding), registry, min_measured=1)
    assert out["coverage"] == [{"stage": "representation", "total": 1, "measured": 0,
                                "closed": False, "thin": False}]
    assert {row["kind"] for row in out["blocking"]} == {"stage_open", "awaiting_rerun"}

    other = _idea(space, binding, "noop")
    _run(registry, other, "noop", params={"incumbent_remeasurement": True})
    out = campaign_coverage(_view(space, registry, binding), registry, min_measured=2)
    assert out["coverage"][0]["measured"] == 0


def test_rejected_dependency_makes_dependent_unreachable_but_retry_does_not(tmp_path):
    space, registry, binding = _fixture(tmp_path)
    parent = _idea(space, binding, "parent")
    _idea(space, binding, "child", depends=(parent,))
    _run(registry, parent, "rejected")
    out = campaign_coverage(_view(space, registry, binding), registry, min_measured=1)
    assert out["done"]


def _promoted_fixture(tmp_path):
    space, registry, binding = _fixture(tmp_path)
    idea = _idea(space, binding, "winner")
    registry.create_run(run_id="winner", experiment_id="campaign", idea_id=idea,
                        stage="representation", family="f", params={}, metrics={},
                        code_ref={"schema_version": 1, "repo": str(REPO), "sha": SHA,
                        "base_sha": SHA, "diff_hash": "a" * 64, "diff_lines": 1},
                        device_fingerprint="cpu", status="running", verdict=None, started_at=1.,
                        finished_at=None, claim_owner="w", heartbeat_at=1.)
    complete_run(registry, run_id="winner", metrics=_metrics())
    artifact = registry.create_artifact(run_id="winner", kind="checkpoint", content=b"weights",
                                        schema_version="1")
    adopt_run_and_promote(registry, run_id="winner", model_id="model", reason="won",
                          model_version={"version": 1, "artifact_id": artifact, "checksum": artifact,
                          "family_version": "f@1", "code_sha": SHA, "preprocessing_hash": "p",
                          "calibration": {}, "thresholds": {},
                          "compat_result": {"head_sha": SHA, "passed": True, "at": 3.},
                          "status": "active"})
    return space, registry, binding, artifact


def test_completion_requires_current_compatible_verified_production_lineage(tmp_path):
    space, registry, binding, artifact = _promoted_fixture(tmp_path)
    before = campaign_completeness(_view(space, registry, binding), registry, min_measured=1)
    assert {row["kind"] for row in before["blocking"]} == {"no_production_alias"}
    RegistryFinalizeService(registry).move_production(model_id="model", version=1, reason="verified")
    assert campaign_completeness(_view(space, registry, binding), registry,
                                 min_measured=1)["done"]
    registry.blobs.path(artifact).write_bytes(b"tampered")
    out = campaign_completeness(_view(space, registry, binding), registry, min_measured=1)
    assert [row["kind"] for row in out["blocking"]] == ["stale_production_artifact"]


@pytest.mark.parametrize("champion_version", [None, 2])
def test_missing_or_wrong_champion_version_is_wrong_lineage(tmp_path, champion_version):
    space, registry, binding, _ = _promoted_fixture(tmp_path)
    RegistryFinalizeService(registry).move_production(model_id="model", version=1, reason="verified")
    with registry._connect("run_superseded") as db:
        db.execute("DROP TRIGGER guard_aliases_delete")
        if champion_version is None:
            db.execute("DELETE FROM aliases WHERE alias='champion'")
        else:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("DROP TRIGGER guard_aliases_update")
            db.execute("UPDATE aliases SET version=? WHERE alias='champion'", (champion_version,))
    out = campaign_completeness(_view(space, registry, binding), registry, min_measured=1)
    assert [item["kind"] for item in out["blocking"]] == ["wrong_production_lineage"]


def test_latest_failed_compatibility_blocks_active_immutable_version(tmp_path):
    space, registry, binding, _ = _promoted_fixture(tmp_path)
    RegistryFinalizeService(registry).move_production(model_id="model", version=1, reason="verified")
    registry.record_compatibility(model_id="model", version=1, head_sha=SHA, passed=False,
                                  reason="implementation changed")
    out = campaign_completeness(_view(space, registry, binding), registry, min_measured=1)
    assert [item["kind"] for item in out["blocking"]] == ["incompatible_production"]


def test_checksum_drift_is_stale_artifact(tmp_path):
    space, registry, binding, _ = _promoted_fixture(tmp_path)
    RegistryFinalizeService(registry).move_production(model_id="model", version=1, reason="verified")
    with registry._connect("run_superseded") as db:
        db.execute("DROP TRIGGER immutable_versions_update")
        db.execute("UPDATE model_versions SET checksum=?", ("0" * 64,))
    out = campaign_completeness(_view(space, registry, binding), registry, min_measured=1)
    assert [item["kind"] for item in out["blocking"]] == ["stale_production_artifact"]


def test_missing_cas_blob_is_stale_artifact(tmp_path):
    space, registry, binding, artifact = _promoted_fixture(tmp_path)
    RegistryFinalizeService(registry).move_production(model_id="model", version=1, reason="verified")
    registry.blobs.path(artifact).unlink()
    out = campaign_completeness(_view(space, registry, binding), registry, min_measured=1)
    assert [item["kind"] for item in out["blocking"]] == ["stale_production_artifact"]


def test_current_head_drift_is_stale_code(tmp_path, monkeypatch):
    space, registry, binding, _ = _promoted_fixture(tmp_path)
    RegistryFinalizeService(registry).move_production(model_id="model", version=1, reason="verified")
    monkeypatch.setattr("knowledge.ml_registry.services.completeness.subprocess.run",
                        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0,
                                                                          stdout="f" * 40 + "\n"))
    out = campaign_completeness(_view(space, registry, binding), registry, min_measured=1)
    assert [item["kind"] for item in out["blocking"]] == ["stale_production_code"]


@pytest.mark.parametrize(("column", "value"), [
    ("run_id", "missing-run"),
    ("status", "superseded"),
])
def test_wrong_or_superseded_version_lineage_blocks(tmp_path, column, value):
    space, registry, binding, _ = _promoted_fixture(tmp_path)
    RegistryFinalizeService(registry).move_production(model_id="model", version=1, reason="verified")
    with registry._connect("run_superseded") as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DROP TRIGGER immutable_versions_update")
        db.execute(f"UPDATE model_versions SET {column}=?", (value,))
    out = campaign_completeness(_view(space, registry, binding), registry, min_measured=1)
    expected = "wrong_production_lineage" if column == "run_id" else "incompatible_production"
    assert [item["kind"] for item in out["blocking"]] == [expected]
