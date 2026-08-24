from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge.ml_registry.contracts import (
    CampaignOutcome,
    CampaignOutcomeRecord,
    ProductionAliasRef,
    StageOutcome,
)
from knowledge.ml_registry.contracts._validation import ContractError
from knowledge.ml_registry.controller import portfolio_schedule
from knowledge.ml_registry.portfolio import CampaignStatus, Portfolio
from knowledge.ml_registry.runtime.campaign_job import CampaignJob, CampaignJobContext
from knowledge.ml_registry.testing.portfolio_fixtures import promoted_artifact_scenario


class _MeasuredCampaign:
    def preflight(self, _context: CampaignJobContext) -> None:
        return None

    def complete(self, _context: CampaignJobContext) -> None:
        return None

    def terminal_outcome(
        self, _context: CampaignJobContext,
    ) -> tuple[CampaignOutcome, str]:
        return CampaignOutcome.MEASURED, "all material families were measured; none may promote"

    def blocking_diagnosis(self, _context: CampaignJobContext) -> None:
        return None

    def trial_count(self, _context: CampaignJobContext) -> int:
        return 0

    def dispatch_one(self, _context: CampaignJobContext) -> tuple[str, ...]:
        raise AssertionError("a terminal measurement must not dispatch another arm")

    def heartbeat(self, _context: CampaignJobContext) -> None:
        return None


def test_non_promoting_campaign_closes_measured_with_a_reason(tmp_path: Path) -> None:
    context = CampaignJobContext("measurement", 1, tmp_path, tmp_path / "progress.json")
    outcome_path = tmp_path / "outcome.json"
    outcome = CampaignJob(
        context=context,
        adapter=_MeasuredCampaign(),
        outcome_path=outcome_path,
        working_directory=tmp_path,
    ).run()

    assert outcome.outcome is CampaignOutcome.MEASURED
    assert outcome.reason == "all material families were measured; none may promote"
    assert json.loads(outcome_path.read_text())["outcome"] == "MEASURED"


@pytest.mark.parametrize(
    "outcome",
    [
        CampaignOutcome.PROMOTED,
        CampaignOutcome.MEASURED,
        CampaignOutcome.REFUTED,
        CampaignOutcome.ABANDONED,
    ],
)
def test_every_terminal_campaign_outcome_requires_a_reason(outcome: CampaignOutcome) -> None:
    production = ProductionAliasRef("model", 1) if outcome is CampaignOutcome.PROMOTED else None
    with pytest.raises(ContractError, match="reason"):
        CampaignOutcomeRecord(
            CampaignOutcomeRecord.VERSION,
            "campaign",
            outcome,
            "",
            1,
            production,
        ).to_mapping()


def test_stage_outcomes_distinguish_vacuous_stagnant_and_advanced() -> None:
    assert StageOutcome.for_stage(material_families=0, completed_families=0, advanced=False) \
        is StageOutcome.VACUOUS
    assert StageOutcome.for_stage(material_families=2, completed_families=2, advanced=False) \
        is StageOutcome.STAGNANT
    assert StageOutcome.for_stage(material_families=2, completed_families=1, advanced=True) \
        is StageOutcome.ADVANCED
    with pytest.raises(ContractError, match="still open"):
        StageOutcome.for_stage(material_families=2, completed_families=1, advanced=False)


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (StageOutcome.ADVANCED, "a material family cleared the rope"),
        (StageOutcome.STAGNANT, "all material families ran; none cleared the rope"),
        (StageOutcome.VACUOUS, "stage has no material families"),
    ],
)
def test_stage_outcome_owns_its_terminal_reason(outcome: StageOutcome, reason: str) -> None:
    assert outcome.reason == reason


def test_claim_carries_concrete_upstream_artifact_version_or_is_refused(tmp_path: Path) -> None:
    scenario = promoted_artifact_scenario(tmp_path)
    portfolio = Portfolio()
    for campaign_id in ("R1", "C1"):
        portfolio.add_campaign(campaign_id, campaign_id).status = CampaignStatus.READY
    specs = ({"id": "R1", "command": ["root"]}, {"id": "C1", "command": ["child"]})

    decision = portfolio_schedule(
        portfolio, specs, {}, {"cpus": 2, "ram_gb": 2}, registry=scenario.registry,
    )
    consumer = next(job for job in decision.jobs if job.campaign_id == "C1")
    assert len(consumer.artifact_pins) == 1
    assert consumer.artifact_pins[0].producer_campaign_id == "R1"
    assert consumer.artifact_pins[0].version == 1
    assert consumer.artifact_pins[0].artifact_id == scenario.fit_artifact_id

    scenario.registry.blobs.path(scenario.fit_artifact_id).unlink()
    refused = portfolio_schedule(
        portfolio, specs, {}, {"cpus": 2, "ram_gb": 2}, registry=scenario.registry,
    )
    assert all(job.campaign_id != "C1" for job in refused.jobs)
    assert "C1" in refused.blocked
