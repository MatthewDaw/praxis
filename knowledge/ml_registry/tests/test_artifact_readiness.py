from __future__ import annotations

import pytest

from knowledge.ml_registry.contracts import CampaignSpec
from knowledge.ml_registry.controller import portfolio_schedule
from knowledge.ml_registry.portfolio import CampaignStatus, Portfolio
from knowledge.ml_registry.services.readiness import ReadinessError, validate_artifact_graph
from knowledge.ml_registry.testing.portfolio_fixtures import (
    _campaign_spec,
    promoted_artifact_scenario,
)


def _spec(campaign_id, *, requires=(), produces=("weights",)):
    value = _campaign_spec(campaign_id, requires=requires)
    value["produces"] = [
        {"artifact_type": item, "schema_version": "1", "oof_for": []}
        for item in produces
    ]
    return CampaignSpec.from_mapping(value)


def test_artifact_graph_is_derived_from_requires_and_produces():
    root = _spec("root", produces=("fit",))
    child = _spec("child", requires=({
        "artifact_type": "fit", "producer_campaign_id": "root",
        "min_coverage": 1.0, "oof": False,
    },))
    validate_artifact_graph((root, child))


@pytest.mark.parametrize("producer", ["missing", "root"])
def test_artifact_graph_refuses_missing_or_ambiguous_contract(producer):
    root = _spec("root", produces=(("other",) if producer == "missing" else ("fit", "fit")))
    child = _spec("child", requires=({
        "artifact_type": "fit", "producer_campaign_id": producer,
        "min_coverage": 1.0, "oof": False,
    },))
    with pytest.raises(ReadinessError, match="unknown producer|exactly one"):
        validate_artifact_graph((root, child))


def test_campaign_specs_are_event_logged_without_adding_a_registry_table(tmp_path):
    scenario = promoted_artifact_scenario(tmp_path)
    assert set(scenario.registry.table_names()) == {
        "experiments", "runs", "artifacts", "registered_models", "model_versions",
        "lineage", "aliases", "events",
    }
    assert [event.event_type for event in scenario.registry.list_events()].count(
        "campaign_spec_registered"
    ) == 2


def test_portfolio_scheduler_gates_on_registry_artifact_readiness(tmp_path):
    scenario = promoted_artifact_scenario(tmp_path)
    scenario.apply("tamper_bytes")
    portfolio = Portfolio()
    for campaign_id in ("R1", "C1"):
        portfolio.add_campaign(campaign_id, campaign_id).status = CampaignStatus.READY
    decision = portfolio_schedule(
        portfolio,
        ({"id": "R1", "command": ["root"]}, {"id": "C1", "command": ["child"]}),
        {}, {"cpus": 2, "ram_gb": 2}, registry=scenario.registry,
    )
    assert [job.campaign_id for job in decision.jobs] == ["R1"]
    assert scenario.fit_artifact_id in decision.blocked["C1"]
    assert "checksum" in decision.blocked["C1"]
