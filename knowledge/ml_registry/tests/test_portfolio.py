"""Portfolio lifecycle, readiness, persistence, and lineage invalidation."""

from __future__ import annotations

import json

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
    portfolio = Portfolio(tmp_path / "portfolio.json")
    portfolio.add_campaign("train-tracking", "tracking")
    downstream = portfolio.add_campaign(
        "train-possession", "possession", [_dependency("tracking-future", "tracking")]
    )

    readiness = portfolio.refresh(downstream.id)
    portfolio.save()

    assert not readiness.activatable
    assert readiness.reasons == ("dependency 'tracking-future' is missing",)
    assert downstream.status == CampaignStatus.BLOCKED
    assert Portfolio.load(portfolio.path).campaigns[downstream.id].status == CampaignStatus.BLOCKED


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
    path = tmp_path / "portfolio.json"
    portfolio = Portfolio(path)
    _artifact(portfolio, "tracking-v1", "tracking")
    campaign = portfolio.add_campaign(
        "possession-campaign", "possession", [_dependency("tracking-v1", "tracking")]
    )
    portfolio.refresh(campaign.id)
    portfolio.save()

    loaded = Portfolio.load(path)

    assert loaded.path == path
    assert loaded.campaigns[campaign.id].status == CampaignStatus.ACTIVATABLE
    assert loaded.campaigns[campaign.id].dependencies == campaign.dependencies
    assert loaded.artifacts["tracking-v1"].coverage == 0.99
    assert json.loads(path.read_text())["schema_version"] == 2


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


# --------------------------------------------------------------------------- P11 lineage


def _chain(portfolio, depth):
    """Register ``depth`` artifacts a0 -> a1 -> ... each carrying explicit lineage."""
    previous = None
    for index in range(depth):
        _artifact(
            portfolio, f"a{index}", f"m{index}",
            input_artifact_ids=() if previous is None else (previous,),
        )
        previous = f"a{index}"
    return previous


@pytest.mark.parametrize("depth", [3, 4])
def test_new_downstream_campaign_added_after_supersession_is_refused(depth):
    portfolio = Portfolio()
    leaf = _chain(portfolio, depth)
    _artifact(portfolio, "a0-next", "m0")
    portfolio.supersede_artifact("a0", "a0-next")

    late = portfolio.add_campaign(
        "late-consumer", "downstream", [_dependency(leaf, f"m{depth - 1}")]
    )
    readiness = portfolio.refresh(late.id)

    assert not readiness.activatable
    assert any("'a0' was superseded by 'a0-next'" in reason for reason in readiness.reasons)
    assert late.status == CampaignStatus.BLOCKED


def test_supersession_stales_consumers_of_a_lineage_descendant_without_a_producer_campaign():
    portfolio = Portfolio()
    _artifact(portfolio, "a-fit", "a")
    _artifact(portfolio, "a-fit-2", "a")
    _artifact(portfolio, "b-fit", "b", input_artifact_ids=("a-fit",))
    consumer = portfolio.add_campaign("consumer", "c", [_dependency("b-fit", "b")])

    affected = portfolio.supersede_artifact("a-fit", "a-fit-2")

    assert affected == {consumer.id}
    assert consumer.stale


def test_lineage_to_a_superseded_artifact_is_refused():
    portfolio = Portfolio()
    _artifact(portfolio, "a-fit", "a")
    _artifact(portfolio, "a-fit-2", "a")
    portfolio.supersede_artifact("a-fit", "a-fit-2")

    with pytest.raises(PortfolioValidationError, match="superseded artifact 'a-fit'"):
        _artifact(portfolio, "b-fit", "b", input_artifact_ids=("a-fit",))


def test_producer_with_dependencies_must_record_them_in_lineage():
    portfolio = Portfolio()
    _artifact(portfolio, "a-fit", "a")
    portfolio.add_campaign("train-b", "b", [_dependency("a-fit", "a")])

    with pytest.raises(PortfolioValidationError, match=r"lineage must include \['a-fit'\]"):
        _artifact(portfolio, "b-fit", "b")

    recorded = _artifact(portfolio, "b-fit", "b", input_artifact_ids=("a-fit",))
    assert recorded.input_artifact_ids == ("a-fit",)


def test_lineage_cycle_in_persisted_state_does_not_hang_readiness(tmp_path):
    path = tmp_path / "portfolio.json"
    portfolio = Portfolio(path)
    _artifact(portfolio, "a-fit", "a")
    _artifact(portfolio, "b-fit", "b", input_artifact_ids=("a-fit",))
    campaign = portfolio.add_campaign("consumer", "c", [_dependency("b-fit", "b")])
    portfolio.save()
    document = json.loads(path.read_text())
    for artifact in document["artifacts"]:
        if artifact["id"] == "a-fit":
            artifact["input_artifact_ids"] = ["b-fit"]
    path.write_text(json.dumps(document))

    loaded = Portfolio.load(path)
    assert loaded.readiness(campaign.id).activatable


def test_absent_lineage_field_is_unknown_and_refuses_readiness(tmp_path):
    path = tmp_path / "portfolio.json"
    portfolio = Portfolio(path)
    _artifact(portfolio, "a-fit", "a")
    campaign = portfolio.add_campaign("consumer", "c", [_dependency("a-fit", "a")])
    portfolio.save()
    document = json.loads(path.read_text())
    for artifact in document["artifacts"]:
        artifact.pop("input_artifact_ids")
    path.write_text(json.dumps(document))

    readiness = Portfolio.load(path).readiness(campaign.id)

    assert not readiness.activatable
    assert any("unknown lineage" in reason for reason in readiness.reasons)


# --------------------------------------------------------------------------- P12 repin


def _stale_portfolio():
    portfolio = Portfolio()
    _artifact(portfolio, "a-fit", "a")
    _artifact(portfolio, "a-fit-2", "a")
    campaign = portfolio.add_campaign("consumer", "c", [_dependency("a-fit", "a")])
    portfolio.refresh(campaign.id)
    portfolio.supersede_artifact("a-fit", "a-fit-2")
    assert campaign.stale
    return portfolio, campaign


def test_repin_clears_stale_when_the_new_ancestry_is_clean():
    portfolio, campaign = _stale_portfolio()

    portfolio.repin(campaign.id, [_dependency("a-fit-2", "a")])

    assert not campaign.stale
    assert campaign.status == CampaignStatus.ACTIVATABLE
    assert campaign.blocked_reasons == []
    assert any(entry.get("event") == "repinned" for entry in campaign.history)


def test_repin_refuses_and_keeps_stale_when_an_ancestor_is_superseded():
    portfolio, campaign = _stale_portfolio()
    _artifact(portfolio, "b-fit", "b", input_artifact_ids=("a-fit-2",))
    _artifact(portfolio, "a-fit-3", "a")
    portfolio.supersede_artifact("a-fit-2", "a-fit-3")

    with pytest.raises(PortfolioValidationError, match="cannot repin"):
        portfolio.repin(campaign.id, [_dependency("b-fit", "b")])
    assert campaign.stale


def test_repin_refuses_a_non_passing_verdict():
    portfolio, campaign = _stale_portfolio()
    _artifact(portfolio, "a-fit-parked", "a", verdict="parked")

    with pytest.raises(PortfolioValidationError, match="not a passing verdict"):
        portfolio.repin(
            campaign.id,
            [_dependency("a-fit-parked", "a", required_verdict="parked")],
        )
    assert campaign.stale


# --------------------------------------------------------------------------- P13/P16 state


@pytest.mark.parametrize("mutation, message", [
    ({"coverage": "abc"}, "must be a number"),
    ({"coverage": True}, "must be a number"),
    ({"coverage": 2.0}, "between 0 and 1"),
    ({"input_artifact_ids": "abc"}, "array of strings"),
    ({"superseded_by": "ghost"}, "unknown artifact 'ghost'"),
    ({"verdict": "shipped"}, "artifact verdict must be one of"),
])
def test_malformed_persisted_artifact_is_refused(tmp_path, mutation, message):
    path = tmp_path / "portfolio.json"
    portfolio = Portfolio(path)
    _artifact(portfolio, "a-fit", "a")
    portfolio.save()
    document = json.loads(path.read_text())
    document["artifacts"][0].update(mutation)
    path.write_text(json.dumps(document))

    with pytest.raises(PortfolioValidationError, match=message):
        Portfolio.load(path)


def test_non_finite_coverage_cannot_round_trip(tmp_path):
    path = tmp_path / "portfolio.json"
    portfolio = Portfolio(path)
    _artifact(portfolio, "a-fit", "a")
    portfolio.save()
    document = json.loads(path.read_text())
    document["artifacts"][0]["coverage"] = 1e999
    path.write_text(json.dumps(document))

    with pytest.raises(PortfolioValidationError, match="finite"):
        Portfolio.load(path)


@pytest.mark.parametrize("mutation, message", [
    ({"history": 5}, "history must be a list"),
    ({"stale": "yes"}, "stale must be a boolean"),
    ({"blocked_reasons": 5}, "blocked_reasons must be an array"),
])
def test_malformed_persisted_campaign_is_refused(tmp_path, mutation, message):
    path = tmp_path / "portfolio.json"
    portfolio = Portfolio(path)
    portfolio.add_campaign("consumer", "c")
    portfolio.save()
    document = json.loads(path.read_text())
    document["campaigns"][0].update(mutation)
    path.write_text(json.dumps(document))

    with pytest.raises(PortfolioValidationError, match=message):
        Portfolio.load(path)


@pytest.mark.parametrize("collection", ["artifacts", "campaigns"])
def test_duplicate_ids_are_refused_rather_than_last_wins(tmp_path, collection):
    path = tmp_path / "portfolio.json"
    portfolio = Portfolio(path)
    _artifact(portfolio, "a-fit", "a")
    portfolio.add_campaign("consumer", "c")
    portfolio.save()
    document = json.loads(path.read_text())
    document[collection].append(dict(document[collection][0]))
    path.write_text(json.dumps(document))

    with pytest.raises(PortfolioValidationError, match="duplicate"):
        Portfolio.load(path)


def test_pre_v2_portfolio_document_is_refused_with_a_migration_message(tmp_path):
    path = tmp_path / "portfolio.json"
    portfolio = Portfolio(path)
    _artifact(portfolio, "a-fit", "a")
    portfolio.save()
    document = json.loads(path.read_text())
    document["schema_version"] = 1
    path.write_text(json.dumps(document))

    with pytest.raises(PortfolioValidationError, match="predates version 2"):
        Portfolio.load(path)


# --------------------------------------------------------------------------- P20 admission


def test_coverage_true_is_not_a_probability():
    portfolio = Portfolio()
    with pytest.raises(PortfolioValidationError, match="must be a number"):
        _artifact(portfolio, "a-fit", "a", coverage=True)


def test_verdict_is_a_closed_enum_on_artifacts_and_dependencies():
    portfolio = Portfolio()
    with pytest.raises(PortfolioValidationError, match="artifact verdict must be one of"):
        _artifact(portfolio, "a-fit", "a", verdict="adoptedd")
    with pytest.raises(PortfolioValidationError, match="required_verdict must be one of"):
        _dependency("a-fit", "a", required_verdict="adoptedd")


def test_minimum_coverage_true_is_refused():
    with pytest.raises(PortfolioValidationError, match="minimum_coverage must be a number"):
        _dependency("a-fit", "a", minimum_coverage=True)


def test_two_campaigns_may_not_produce_the_same_model():
    portfolio = Portfolio()
    portfolio.add_campaign("first", "shared")
    with pytest.raises(PortfolioValidationError, match="at most one campaign"):
        portfolio.add_campaign("second", "shared")
    assert set(portfolio.campaigns) == {"first"}


def test_portfolio_verdicts_match_the_single_campaign_registry():
    from knowledge.ml_registry import verdict as verdict_module
    from knowledge.ml_registry.portfolio import VERDICTS

    assert VERDICTS == {
        verdict_module.VERDICT_ADOPTED,
        verdict_module.VERDICT_PARKED,
        verdict_module.VERDICT_REJECTED,
        verdict_module.VERDICT_VOIDED,
    }
