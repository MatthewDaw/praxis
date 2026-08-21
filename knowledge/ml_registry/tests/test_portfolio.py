"""Portfolio lifecycle, readiness, persistence, and lineage invalidation."""

from __future__ import annotations

import pytest

from knowledge.ml_registry.portfolio import (
    ArtifactDependency,
    CampaignStatus,
    Portfolio,
    PortfolioValidationError,
)


def _artifact(portfolio: Portfolio, artifact_id: str, model_id: str, **overrides):
    values = {
        "verdict": "adopted",
        "dataset_manifest_hash": f"data-{artifact_id}",
        "split_manifest_hash": f"split-{artifact_id}",
        "prediction_manifest_hash": f"pred-{artifact_id}",
        "coverage": 0.99,
    }
    values.update(overrides)
    return portfolio.register_artifact(artifact_id, model_id, **values)


def _dependency(artifact_id: str, model_id: str, **overrides):
    values = {
        "upstream_model_id": model_id,
        "artifact_id": artifact_id,
        "required_verdict": "adopted",
        "dataset_manifest_hash": f"data-{artifact_id}",
        "split_manifest_hash": f"split-{artifact_id}",
        "prediction_manifest_hash": f"pred-{artifact_id}",
        "minimum_coverage": 0.95,
    }
    values.update(overrides)
    return ArtifactDependency(**values)


def test_full_lifecycle_requires_readiness_and_seeding():
    portfolio = Portfolio()
    _artifact(portfolio, "tracking-v1", "tracking")
    campaign = portfolio.add_campaign(
        "possession-campaign", "possession", [_dependency("tracking-v1", "tracking")]
    )

    assert campaign.status == CampaignStatus.PLANNED
    assert portfolio.refresh(campaign.id).activatable
    assert campaign.status == CampaignStatus.ACTIVATABLE
    portfolio.start_seeding(campaign.id)
    assert campaign.status == CampaignStatus.SEEDING
    portfolio.mark_ready(campaign.id)
    assert campaign.status == CampaignStatus.READY
    assert [entry["to"] for entry in campaign.history if "to" in entry] == [
        "ACTIVATABLE", "SEEDING", "READY"
    ]


def test_readiness_reports_every_manifest_verdict_and_coverage_mismatch():
    portfolio = Portfolio()
    _artifact(
        portfolio, "tracking-v1", "tracking", verdict="parked", coverage=0.5,
        dataset_manifest_hash="wrong-data", split_manifest_hash="wrong-split",
        prediction_manifest_hash="wrong-pred",
    )
    campaign = portfolio.add_campaign(
        "possession-campaign", "possession", [_dependency("tracking-v1", "tracking")]
    )

    readiness = portfolio.refresh(campaign.id)

    assert not readiness.activatable
    assert campaign.status == CampaignStatus.BLOCKED
    assert len(readiness.reasons) == 5
    assert any("verdict" in reason for reason in readiness.reasons)
    assert any("dataset manifest" in reason for reason in readiness.reasons)
    assert any("split manifest" in reason for reason in readiness.reasons)
    assert any("prediction manifest" in reason for reason in readiness.reasons)
    assert any("coverage" in reason for reason in readiness.reasons)
    with pytest.raises(PortfolioValidationError, match="ACTIVATABLE"):
        portfolio.start_seeding(campaign.id)


def test_dependency_validation_refuses_dangling_artifacts_and_wrong_owner():
    portfolio = Portfolio()
    with pytest.raises(PortfolioValidationError, match="dangling dependency"):
        portfolio.add_campaign("downstream", "value", [_dependency("missing", "tracking")])
    assert "downstream" not in portfolio.campaigns

    _artifact(portfolio, "tracking-v1", "tracking")
    with pytest.raises(PortfolioValidationError, match="does not own"):
        portfolio.add_campaign("downstream", "value", [_dependency("tracking-v1", "geometry")])


def test_planned_dependency_may_wait_for_artifact_from_declared_upstream_campaign(tmp_path):
    portfolio = Portfolio()
    portfolio.add_campaign("train-tracking", "tracking")
    downstream = portfolio.add_campaign(
        "train-possession", "possession", [_dependency("tracking-future", "tracking")]
    )

    readiness = portfolio.refresh(downstream.id)

    assert not readiness.activatable
    assert readiness.reasons == ("dependency 'tracking-future' is missing",)
    assert downstream.status == CampaignStatus.BLOCKED


def test_cycle_is_refused_without_leaving_partial_campaign():
    portfolio = Portfolio()
    _artifact(portfolio, "a-fit", "a")
    _artifact(portfolio, "b-fit", "b")
    portfolio.add_campaign("train-a", "a", [_dependency("b-fit", "b")])

    with pytest.raises(PortfolioValidationError, match="cycle"):
        portfolio.add_campaign("train-b", "b", [_dependency("a-fit", "a")])
    assert set(portfolio.campaigns) == {"train-a"}


def test_supersession_recursively_stales_downstream_but_preserves_history():
    portfolio = Portfolio()
    _artifact(portfolio, "tracking-v1", "tracking")
    _artifact(portfolio, "tracking-v2", "tracking", coverage=1.0)
    _artifact(portfolio, "possession-v1", "possession")
    _artifact(portfolio, "unrelated-v1", "unrelated")
    possession = portfolio.add_campaign(
        "train-possession", "possession", [_dependency("tracking-v1", "tracking")]
    )
    value = portfolio.add_campaign(
        "train-value", "value", [_dependency("possession-v1", "possession")]
    )
    unrelated = portfolio.add_campaign(
        "train-unrelated-consumer", "consumer", [_dependency("unrelated-v1", "unrelated")]
    )
    for campaign in (possession, value, unrelated):
        portfolio.refresh(campaign.id)
        portfolio.start_seeding(campaign.id)
        portfolio.mark_ready(campaign.id)

    affected = portfolio.supersede_artifact("tracking-v1", "tracking-v2")

    assert affected == {"train-possession", "train-value"}
    assert portfolio.artifacts["tracking-v1"].superseded_by == "tracking-v2"
    assert portfolio.artifacts["tracking-v1"].current is False
    for campaign in (possession, value):
        assert campaign.status == CampaignStatus.BLOCKED
        assert campaign.stale
        assert any(entry.get("from") == "READY" for entry in campaign.history)
    assert unrelated.status == CampaignStatus.READY
    assert not unrelated.stale


def test_exact_replacement_dependency_remains_independently_current():
    portfolio = Portfolio()
    _artifact(portfolio, "tracking-v1", "tracking")
    _artifact(portfolio, "tracking-v2", "tracking")
    old_consumer = portfolio.add_campaign(
        "old-consumer", "old-downstream", [_dependency("tracking-v1", "tracking")]
    )
    new_consumer = portfolio.add_campaign(
        "new-consumer", "new-downstream", [_dependency("tracking-v2", "tracking")]
    )

    affected = portfolio.supersede_artifact("tracking-v1", "tracking-v2")

    assert affected == {old_consumer.id}
    assert new_consumer.status == CampaignStatus.PLANNED
    assert not new_consumer.stale


def test_json_round_trip_is_durable_and_human_readable(tmp_path):
    from knowledge.ml_registry.storage.projections import PortfolioProjectionSpec
    from knowledge.ml_registry.storage.registry import Registry
    from knowledge.ml_registry.tests.artifact_projection_fixture import render_legacy_artifact_views

    render_legacy_artifact_views(tmp_path)
    loaded = Portfolio.from_registry(
        Registry(tmp_path / "canonical_registry"),
        portfolio_spec=PortfolioProjectionSpec(1, ()),
    )
    assert loaded.path is None
    assert loaded.artifacts["artifact-weights-v1"].coverage == 0.9
    with pytest.raises(PortfolioValidationError, match="read-only"):
        loaded.add_campaign("forbidden", "model")


@pytest.mark.parametrize("coverage", [-0.01, 1.01])
def test_coverage_is_a_probability(coverage):
    portfolio = Portfolio()
    with pytest.raises(PortfolioValidationError, match="between 0 and 1"):
        _artifact(portfolio, "bad", "model", coverage=coverage)


def test_supersession_requires_same_model_and_preserves_old_artifact_on_refusal():
    portfolio = Portfolio()
    old = _artifact(portfolio, "tracking-v1", "tracking")
    _artifact(portfolio, "geometry-v1", "geometry")

    with pytest.raises(PortfolioValidationError, match="same model"):
        portfolio.supersede_artifact("tracking-v1", "geometry-v1")
    assert old.current
