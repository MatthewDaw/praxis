from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from knowledge.ml_registry.completeness import campaign_completeness
from knowledge.ml_registry.contracts import CampaignArtifact, PromotionRecord
from knowledge.ml_registry.lifecycle import adopt_idea
from knowledge.ml_registry.services.finalize import (
    FinalizationError,
    FinalizationRequest,
    Finalizer,
)
from knowledge.ml_registry.services.ratchet import ACTIVE_LINEAGE_FIELD, record_adoption_lineage
from knowledge.ml_registry.storage import ArtifactStore
from knowledge.ml_registry.write_path import RegistrySpace, register_idea, register_model, register_trial


STAGES = ("representation", "architecture", "tuning")


def _campaign(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    space = RegistrySpace()
    model_id = register_model(space, {
        "metric": "f1", "direction": "maximize", "win_condition": {"metric_at_least": 0.9},
        "baseline": "base", "noise_floor": 0.01, "baseline_throughput": 1.0,
        "diff_size_limit": 8, "max_trials": 20, "max_discovered_ideas": 0,
    })
    adopted_trial_id = ""
    for index, stage in enumerate(STAGES):
        idea_id = register_idea(space, {
            "model_id": model_id, "origin": "seeded", "axis": stage,
            "description": f"{stage}-arm", "id": f"arm-{index}",
        })
        trial_id = register_trial(space, {
            "model_id": model_id, "idea_id": idea_id, "commit": f"commit-{index}",
            "status": "complete", "throughput": 1.0, "diff_lines": 1,
        }, frozenset({f"commit-{index}"}))
        space.get(trial_id).meta.update(status="succeeded")
        if stage == "tuning":
            adopt_idea(space, idea_id, trial_id)
            adopted_trial_id = trial_id

    lineage = record_adoption_lineage(
        model_id, space.get(model_id).meta, idea_id=idea_id, trial_id=adopted_trial_id,
        adopted_commit="commit-2", parent_baseline_commit="base",
    )

    source = tmp_path / "weights.bin"
    source.write_bytes(b"converged weights")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    store = ArtifactStore(tmp_path / "artifact-store")
    artifact = CampaignArtifact.from_mapping({
        "schema_version": 1, "artifact_id": "fit-1", "artifact_type": "weights",
        "uri": source.resolve().as_uri(), "sha256": digest, "size_bytes": source.stat().st_size,
        "producer_campaign_id": "campaign-1", "trial_id": adopted_trial_id,
        "lineage_id": lineage.lineage_id, "interface_version": "v1",
    })
    store.ingest_artifact(source, artifact)
    promotion = PromotionRecord.from_mapping({
        "schema_version": 1, "promotion_record_id": "promotion-1",
        "campaign_id": "campaign-1", "model_id": model_id,
        "adopted_trial_id": adopted_trial_id, "lineage_id": lineage.lineage_id,
        "convergence_artifact_id": "fit-1", "dataset_manifest_hash": "dataset",
        "split_manifest_hash": "split", "preprocessing_hash": "preprocessing",
        "code_commit": "code", "configuration_hash": "configuration", "metric_name": "f1",
        "metric_value": 0.91, "thresholds_hash": "thresholds", "upstream_artifact_ids": [],
        "compatibility_test": "fixture_loader:load", "compatibility_passed": True,
    })
    request = FinalizationRequest(
        promotion=promotion, stages=STAGES, min_measured=1,
        result={"status": "complete"}, verdict={"status": "adopted"},
        baseline={"metric_value": 0.91}, readiness={"ready": True},
    )
    return space, model_id, store, request


def test_finalize_writes_one_promotion_and_canonical_completeness_accepts_it(
    tmp_path: Path,
) -> None:
    space, model_id, store, request = _campaign(tmp_path)
    calls: list[tuple[str, str]] = []

    def compatibility(test_name, artifact):
        calls.append((test_name, artifact.artifact_id))
        return True

    finalizer = Finalizer(store, compatibility)
    first = finalizer.finalize(space, request)
    second = finalizer.finalize(space, request)

    assert first == second == request.promotion
    assert finalizer.verify(space, first) == first
    assert calls == [("fixture_loader:load", "fit-1")] * 3
    assert campaign_completeness(
        space, model_id, STAGES, min_measured=1, promotion_source=store,
    )["done"]
    assert [event.event_type for event in store.replay().events] == [
        "artifact_ingested", "campaign_finalized",
    ]


@pytest.mark.parametrize("checkpoint", [
    "after_completeness", "after_artifact_verification", "after_lineage_verification",
    "after_upstream_verification", "after_compatibility",
])
def test_every_precommit_failpoint_leaves_no_partial_finalization(
    tmp_path: Path, checkpoint: str,
) -> None:
    space, _, store, request = _campaign(tmp_path)

    def failpoint(name):
        if name == checkpoint:
            raise RuntimeError(f"failed at {name}")

    with pytest.raises(RuntimeError, match=checkpoint):
        Finalizer(store, lambda _name, _artifact: True, failpoint=failpoint).finalize(
            space, request,
        )
    snapshot = store.replay()
    assert snapshot.promotions == {}
    assert [event.event_type for event in snapshot.events] == ["artifact_ingested"]


def test_failure_after_commit_is_recovered_by_idempotent_retry(tmp_path: Path) -> None:
    space, _, store, request = _campaign(tmp_path)
    failures = 1

    def failpoint(name):
        nonlocal failures
        if name == "after_commit" and failures:
            failures -= 1
            raise RuntimeError("lost acknowledgement")

    finalizer = Finalizer(store, lambda _name, _artifact: True, failpoint=failpoint)
    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        finalizer.finalize(space, request)
    assert finalizer.finalize(space, request) == request.promotion
    assert len(store.replay().promotions) == 1


def test_idempotent_retry_refuses_changed_finalization_payload(tmp_path: Path) -> None:
    space, _, store, request = _campaign(tmp_path)
    finalizer = Finalizer(store, lambda _name, _artifact: True)
    finalizer.finalize(space, request)

    with pytest.raises(FinalizationError, match="retry drifted"):
        finalizer.finalize(space, replace(request, readiness={"ready": False}))
    assert len(store.replay().promotions) == 1


def test_finalize_rejects_wrong_current_lineage_without_writing(tmp_path: Path) -> None:
    space, _, store, request = _campaign(tmp_path)
    wrong = replace(
        request,
        promotion=replace(request.promotion, adopted_trial_id="superseded-trial"),
    )

    with pytest.raises(FinalizationError, match="artifact is not bound to the adopted trial"):
        Finalizer(store, lambda _name, _artifact: True).finalize(space, wrong)
    assert store.promotion_for_campaign("campaign-1") is None


def test_finalize_rejects_a_promotion_after_its_adoption_lineage_changed(
    tmp_path: Path,
) -> None:
    space, model_id, store, request = _campaign(tmp_path)
    space.get(model_id).meta[ACTIVE_LINEAGE_FIELD] = "adoption:newer-trial"

    with pytest.raises(FinalizationError, match="current adoption lineage"):
        Finalizer(store, lambda _name, _artifact: True).finalize(space, request)
    assert store.promotion_for_campaign("campaign-1") is None


def test_finalize_rejects_tampered_artifact_upstream_and_compatibility(
    tmp_path: Path,
) -> None:
    space, _, store, request = _campaign(tmp_path)
    artifact = store.artifact("fit-1")
    Path(artifact.uri.removeprefix("file://")).write_bytes(b"tampered")
    with pytest.raises(FinalizationError, match="verification failed"):
        Finalizer(store, lambda _name, _artifact: True).finalize(space, request)

    space, _, store, request = _campaign(tmp_path / "upstream")
    missing_upstream = replace(
        request,
        promotion=replace(request.promotion, upstream_artifact_ids=("missing",)),
    )
    with pytest.raises(FinalizationError, match="upstream artifact 'missing'"):
        Finalizer(store, lambda _name, _artifact: True).finalize(space, missing_upstream)

    with pytest.raises(FinalizationError, match="did not pass"):
        Finalizer(store, lambda _name, _artifact: False).finalize(space, request)
    assert store.promotion_for_campaign("campaign-1") is None
