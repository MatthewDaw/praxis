from __future__ import annotations

from pathlib import Path

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
