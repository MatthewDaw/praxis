from __future__ import annotations

from pathlib import Path

import pytest

from knowledge.ml_registry.artifact_cache import ArtifactCacheIndex, save_index
from knowledge.ml_registry.manifests import ManifestRegistry, ManifestValidationError
from knowledge.ml_registry.portfolio import Portfolio, PortfolioValidationError
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.storage.projections import (
    LegacyArtifactDependency,
    LegacyCampaignProjection,
    PortfolioProjectionSpec,
)
from knowledge.ml_registry.tests.artifact_projection_fixture import (
    canonical_json,
    render_legacy_artifact_views,
)


GOLDENS = Path(__file__).with_name("fixtures") / "artifact_projections"


def test_one_canonical_artifact_lineage_reproduces_all_three_legacy_views_byte_exactly(
    tmp_path: Path,
) -> None:
    actual = render_legacy_artifact_views(tmp_path)
    expected = {
        name: (GOLDENS / f"{name}.json").read_bytes()
        for name in actual
    }
    assert actual == expected


def test_canonical_views_bind_the_same_manifest_lineage(tmp_path: Path) -> None:
    views = {name: canonical_json(content)
             for name, content in render_legacy_artifact_views(tmp_path).items()}
    dataset_hash = views["manifest_registry"]["datasets"][0]["hash"]
    split_hash = views["manifest_registry"]["splits"][0]["hash"]
    prediction_hash = views["manifest_registry"]["predictions"][0]["hash"]
    cache_key = next(iter(views["artifact_cache_index"]["entries"].values()))["key"]
    artifact = views["portfolio_artifacts"]["artifacts"][0]
    assert cache_key["dataset_manifest"] == artifact["dataset_manifest_hash"] == dataset_hash
    assert cache_key["split"] == artifact["split_manifest_hash"] == split_hash
    assert artifact["prediction_manifest_hash"] == prediction_hash


def test_projection_preserves_superseded_history_and_uses_alias_for_active(
    tmp_path: Path,
) -> None:
    views = {name: canonical_json(content) for name, content in
             render_legacy_artifact_views(tmp_path, include_history=True).items()}
    cache = views["artifact_cache_index"]
    portfolio = views["portfolio_artifacts"]
    assert len(cache["entries"]) == 2
    assert len(cache["active"]) == 1
    assert len(cache["superseded"]) == 1
    old, current = portfolio["artifacts"]
    assert old["id"] == "artifact-weights-v1"
    assert old["superseded_by"] == current["id"] == "artifact-weights-v2"
    assert old["superseded_at"] == current["created_at"]
    assert portfolio["campaigns"][0]["status"] == "BLOCKED"
    assert "was superseded" in portfolio["campaigns"][0]["blocked_reasons"][0]


def test_legacy_objects_read_canonical_projection_documents_without_owning_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = {name: canonical_json(content) for name, content in
                 render_legacy_artifact_views(tmp_path).items()}
    monkeypatch.setattr(
        "knowledge.ml_registry.storage.projections.project_manifest_registry",
        lambda _registry: documents["manifest_registry"],
    )
    monkeypatch.setattr(
        "knowledge.ml_registry.storage.projections.project_artifact_cache_index",
        lambda _registry: documents["artifact_cache_index"],
    )
    monkeypatch.setattr(
        "knowledge.ml_registry.storage.projections.project_portfolio_artifacts",
        lambda _registry, *, portfolio_spec: documents["portfolio_artifacts"],
    )
    projection_spec = PortfolioProjectionSpec(1, (
        LegacyCampaignProjection("consumer-canonical", "model-consumer", (
            LegacyArtifactDependency(
                "model-canonical", "artifact-weights-v1", "adopted",
                documents["manifest_registry"]["datasets"][0]["hash"],
                documents["manifest_registry"]["splits"][0]["hash"],
                documents["manifest_registry"]["predictions"][0]["hash"], .9,
            ),
        )),
    ))
    registry = object()

    manifests = ManifestRegistry.from_registry(registry)  # type: ignore[arg-type]
    cache = ArtifactCacheIndex.from_registry(registry)  # type: ignore[arg-type]
    portfolio = Portfolio.from_registry(registry, portfolio_spec=projection_spec)  # type: ignore[arg-type]

    assert manifests.datasets["dataset-canonical"].hash == documents["manifest_registry"]["datasets"][0]["hash"]
    assert cache.to_dict() == documents["artifact_cache_index"]
    assert portfolio.artifacts["artifact-weights-v1"].coverage == .9
    assert portfolio.readiness("consumer-canonical").activatable


def test_registry_backed_legacy_views_refuse_every_persistence_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = {name: canonical_json(content) for name, content in
                 render_legacy_artifact_views(tmp_path).items()}
    monkeypatch.setattr("knowledge.ml_registry.storage.projections.project_manifest_registry",
                        lambda _registry: documents["manifest_registry"])
    monkeypatch.setattr("knowledge.ml_registry.storage.projections.project_artifact_cache_index",
                        lambda _registry: documents["artifact_cache_index"])
    monkeypatch.setattr("knowledge.ml_registry.storage.projections.project_portfolio_artifacts",
                        lambda _registry, *, portfolio_spec: documents["portfolio_artifacts"])
    spec = PortfolioProjectionSpec(1, ())
    registry = object()
    manifests = ManifestRegistry.from_registry(registry)  # type: ignore[arg-type]
    cache = ArtifactCacheIndex.from_registry(registry)  # type: ignore[arg-type]
    portfolio = Portfolio.from_registry(registry, portfolio_spec=spec)  # type: ignore[arg-type]

    with pytest.raises(ManifestValidationError, match="read-only"):
        manifests.save()
    with pytest.raises(RegistryValidationError, match="read-only"):
        cache.invalidate(next(iter(cache.entries)), reason="must route canonically")
    with pytest.raises(RegistryValidationError, match="read-only"):
        save_index(tmp_path / "forbidden-cache.json", cache)
    with pytest.raises(PortfolioValidationError, match="read-only"):
        portfolio.save()
    assert not (tmp_path / "forbidden-cache.json").exists()
