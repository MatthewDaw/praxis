from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from knowledge.ml_registry import Registry
from knowledge.ml_registry.domain import (Alias, CampaignBinding, CampaignView, IdeaInventory,
                                          ModelVersion)
from knowledge.ml_registry.services.registry_finalize import (
    RegistryFinalizationError,
    RegistryFinalizer,
)
from knowledge.ml_registry.storage.registry import _PRODUCTION_CAPABILITY
from knowledge.ml_registry.tests.test_registry_native_adjudication import registry_with_champion


def view(registry: Registry) -> CampaignView:
    run = registry.rows("runs")[0]
    fact = SimpleNamespace(id=run["idea_id"])
    return CampaignView(
        CampaignBinding("campaign", "model", "model-fact"), registry.rows("experiments")[0],
        registry.rows("registered_models")[0], SimpleNamespace(id="model-fact"),
        (IdeaInventory(fact, run["idea_id"], run["stage"], (), (run,)),),
    )


def finalizer(registry: Registry, *, loads=True) -> RegistryFinalizer:
    return RegistryFinalizer(
        registry,
        compatibility_loader=lambda _version, path, _head: path.read_bytes() == b"base" and loads,
        min_measured=1,
    )


def test_finalization_is_one_registry_event_and_returns_canonical_views(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    before = len(registry.list_events())

    result = finalizer(registry).finalize(view(registry), version=1, reason="release")

    assert isinstance(result.model_version, ModelVersion)
    assert isinstance(result.production_alias, Alias)
    assert result.production_alias.alias == "production"
    assert result.production_alias.set_by == "finalize"
    events = registry.list_events()[before:]
    assert [event.event_type for event in events] == ["registry_finalized"]
    assert events[0].payload["artifact_id"] == result.model_version.artifact_id


def test_full_payload_retry_is_idempotent_and_drift_is_refused(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    service = finalizer(registry)
    campaign = view(registry)
    first = service.finalize(campaign, version=1, reason="release")
    count = len(registry.list_events())
    assert service.finalize(campaign, version=1, reason="release") == first
    assert len(registry.list_events()) == count
    with pytest.raises(RegistryFinalizationError, match="full semantic payload"):
        service.finalize(campaign, version=1, reason="different release")


def test_crash_after_event_recovers_alias_and_finalization_together(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)

    def crash(event):
        if event.event_type == "registry_finalized":
            raise RuntimeError("crash after finalization event")

    registry.after_event = crash
    with pytest.raises(RuntimeError, match="crash after finalization event"):
        finalizer(registry).finalize(view(registry), version=1, reason="release")
    assert not any(row["alias"] == "production" for row in registry.rows("aliases"))

    recovered = Registry(tmp_path)
    result = finalizer(recovered).finalize(view(recovered), version=1, reason="release")
    assert result.production_alias.version == 1
    assert len([event for event in recovered.list_events()
                if event.event_type == "registry_finalized"]) == 1


def test_completeness_compatibility_and_blob_are_hard_gates(tmp_path: Path) -> None:
    incomplete = registry_with_champion(tmp_path / "incomplete")
    campaign = view(incomplete)
    missing = SimpleNamespace(id="missing-idea")
    campaign = CampaignView(campaign.binding, campaign.experiment, campaign.registered_model,
                            campaign.model_fact,
                            campaign.ideas + (IdeaInventory(missing, "missing", "representation", (), ()),))
    with pytest.raises(RegistryFinalizationError, match="coverage"):
        finalizer(incomplete).finalize(campaign, version=1, reason="release")

    incompatible = registry_with_champion(tmp_path / "incompatible")
    with pytest.raises(RegistryFinalizationError, match="compatibility load"):
        finalizer(incompatible, loads=False).finalize(view(incompatible), version=1, reason="release")

    tampered = registry_with_champion(tmp_path / "tampered")
    artifact_id = tampered.rows("model_versions")[0]["artifact_id"]
    tampered.blobs.path(artifact_id).write_bytes(b"tampered")
    with pytest.raises(RegistryFinalizationError, match="blob verification"):
        finalizer(tampered).finalize(view(tampered), version=1, reason="release")


def test_full_completeness_must_close_after_atomic_production_event(
    tmp_path: Path, monkeypatch,
) -> None:
    registry = registry_with_champion(tmp_path)
    monkeypatch.setattr("knowledge.ml_registry.services.registry_finalize.campaign_completeness",
                        lambda *_args, **_kwargs: {"done": False})
    with pytest.raises(RegistryFinalizationError, match="did not close"):
        finalizer(registry).finalize(view(registry), version=1, reason="release")
    assert any(event.event_type == "registry_finalized" for event in registry.list_events())


def test_only_current_champion_adopted_lineage_can_finalize(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    # The canonical champion succeeds; a nonexistent or non-champion version never reaches compatibility.
    with pytest.raises(RegistryFinalizationError, match="current champion"):
        finalizer(registry).finalize(view(registry), version=2, reason="release")


def test_finalization_target_must_match_campaign_view_binding(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    campaign = view(registry)
    mismatched = CampaignView(CampaignBinding("other", "model", "model-fact"),
                              campaign.experiment, campaign.registered_model,
                              campaign.model_fact, campaign.ideas)
    with pytest.raises(RegistryFinalizationError, match="experiment/model binding"):
        finalizer(registry).finalize(mismatched, version=1, reason="release")


def test_head_move_during_load_refuses_publication(tmp_path: Path, monkeypatch) -> None:
    registry = registry_with_champion(tmp_path)
    heads = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr(registry, "_git_head", lambda _repo: next(heads))
    with pytest.raises(RegistryFinalizationError, match="HEAD moved"):
        finalizer(registry).finalize(view(registry), version=1, reason="release")


def test_finalization_compatibility_overlays_adoption_head_and_later_head_is_stale(
    tmp_path: Path, monkeypatch,
) -> None:
    registry = registry_with_champion(tmp_path)
    campaign = view(registry)
    head_b = "b" * 40
    head_c = "c" * 40
    current = {"head": head_b}
    monkeypatch.setattr(registry, "_git_head", lambda _repo: current["head"])

    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if command[:3] == ["git", "-C", str(Path(__file__).resolve().parents[3])]:
            return SimpleNamespace(stdout=current["head"] + "\n", returncode=0)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr("knowledge.ml_registry.services.completeness.subprocess.run", fake_run)
    result = finalizer(registry).finalize(campaign, version=1, reason="compatible at B")
    assert result.model_version.compat_result["head_sha"] == head_b
    assert registry.effective_model_version("model", 1)["effective_compat_result"]["head_sha"] == head_b

    current["head"] = head_c
    from knowledge.ml_registry.services.completeness import campaign_completeness
    state = campaign_completeness(campaign, registry, min_measured=1)
    assert state["done"] is False
    assert state["blocking"][0]["kind"] == "stale_production_code"
    with pytest.raises(RegistryFinalizationError, match="stale for current HEAD"):
        finalizer(registry).verify(campaign, version=1)


def test_pending_projection_refuses_champion_race_before_event_append(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    before = len(registry.list_events())
    payload = {"model_id": "model", "version": 999, "run_id": "baseline",
               "artifact_id": registry.rows("model_versions")[0]["artifact_id"],
               "checksum": registry.rows("model_versions")[0]["checksum"], "head_sha": "a" * 40,
               "reason": "stale target", "upstreams": []}
    with pytest.raises(RegistryFinalizationError, match="current champion"):
        try:
            registry._finalize_registry_version(payload, capability=_PRODUCTION_CAPABILITY)
        except Exception as exc:
            raise RegistryFinalizationError(str(exc)) from exc
    assert len(registry.list_events()) == before


def test_pending_projection_refuses_moved_upstream_before_event_append(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    before = len(registry.list_events())
    version = registry.rows("model_versions")[0]
    payload = {"model_id": "model", "version": 1, "run_id": "baseline",
               "artifact_id": version["artifact_id"], "checksum": version["checksum"],
               "head_sha": "a" * 40, "reason": "stale upstream",
               "upstreams": [{"model_id": "model", "version": 1,
                              "artifact_id": version["artifact_id"], "checksum": version["checksum"],
                              "kind": "backbone"}]}
    with pytest.raises(RegistryFinalizationError, match="upstream production alias moved"):
        try:
            registry._finalize_registry_version(payload, capability=_PRODUCTION_CAPABILITY)
        except Exception as exc:
            raise RegistryFinalizationError(str(exc)) from exc
    assert len(registry.list_events()) == before
