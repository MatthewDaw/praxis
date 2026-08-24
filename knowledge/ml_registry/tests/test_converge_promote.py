from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledge.ml_registry.domain import CampaignBinding, CampaignView, IdeaInventory
from knowledge.ml_registry.services.registry_finalize import (
    ConvergePromoter,
    FinalizedModel,
    LandingCommitWriter,
    RegistryFinalizationError,
    RegistryFinalizeService,
)
from knowledge.ml_registry.storage import Registry
from knowledge.ml_registry.tests.test_registry_native_adjudication import (
    create_run,
    promotion,
    registry_with_champion,
)


def _view(registry: Registry) -> CampaignView:
    runs = registry.rows("runs")
    ideas = tuple(
        IdeaInventory(SimpleNamespace(id=row["idea_id"]), row["idea_id"], row["stage"], (), (row,))
        for row in runs
    )
    return CampaignView(
        CampaignBinding("campaign", "model", "model-fact"),
        registry.rows("experiments")[0],
        registry.rows("registered_models")[0],
        SimpleNamespace(id="model-fact"),
        ideas,
    )


def _promoter(registry: Registry, landing_commit: LandingCommitWriter) -> ConvergePromoter:
    return ConvergePromoter(
        registry,
        compatibility_loader=lambda _version, path, _head: path.read_bytes() == b"winner:converge",
        landing_commit=landing_commit,
        min_measured=1,
    )


def _baseline_in_production(registry: Registry) -> None:
    RegistryFinalizeService(registry).move_production(
        model_id="model", version=1, reason="existing production",
    )


def test_converge_promotes_both_aliases_and_reports_landing_commit(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    _baseline_in_production(registry)
    create_run(registry, "converge", .72)

    result = _promoter(registry, lambda finalized: f"commit-{finalized.model_version.version}").run(
        _view(registry), run_id="converge", version=2, reason="full-length winner",
        promotion=promotion(registry, "converge"),
    )

    assert result.verdict == "adopted"
    assert result.landing_commit == "commit-2"
    assert {row["alias"]: row["version"] for row in registry.rows("aliases")} == {
        "champion": 2,
        "production": 2,
    }


def test_failed_landing_commit_restores_both_aliases(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    _baseline_in_production(registry)
    create_run(registry, "converge", .72)

    def fail_landing(_finalized: FinalizedModel) -> str:
        raise RuntimeError("landing writer failed")

    with pytest.raises(RegistryFinalizationError, match="landing writer failed"):
        _promoter(registry, fail_landing).run(
            _view(registry), run_id="converge", version=2, reason="full-length winner",
            promotion=promotion(registry, "converge"),
        )

    assert {row["alias"]: row["version"] for row in registry.rows("aliases")} == {
        "champion": 1,
        "production": 1,
    }


def test_converge_that_does_not_beat_predecessor_keeps_production(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    _baseline_in_production(registry)
    create_run(registry, "converge", .685)
    calls: list[FinalizedModel] = []

    result = _promoter(registry, lambda finalized: calls.append(finalized) or "unexpected").run(
        _view(registry), run_id="converge", version=2, reason="full-length comparison",
    )

    assert result.verdict == "parked"
    assert result.landing_commit is None
    assert calls == []
    assert {row["alias"]: row["version"] for row in registry.rows("aliases")} == {
        "champion": 1,
        "production": 1,
    }


def test_shared_family_requires_a_measurement_for_every_consumer(tmp_path: Path) -> None:
    registry = registry_with_champion(tmp_path)
    _baseline_in_production(registry)
    create_run(registry, "converge", .72)

    with pytest.raises(RegistryFinalizationError, match="missing consumer measurements: basketball"):
        _promoter(registry, lambda _finalized: "unexpected").run(
            _view(registry), run_id="converge", version=2, reason="shared winner",
            promotion=promotion(registry, "converge"),
            consuming_sports=("baseball", "basketball"), measured_sports=("baseball",),
        )

    assert {row["alias"]: row["version"] for row in registry.rows("aliases")} == {
        "champion": 1,
        "production": 1,
    }
