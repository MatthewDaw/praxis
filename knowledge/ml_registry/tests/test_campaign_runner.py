from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from knowledge.ml_registry.contracts import CampaignOutcome, CampaignOutcomeRecord
from knowledge.ml_registry.runner import (
    CampaignDispatch,
    register_campaign_for_run,
    run_registered_campaigns,
)
from knowledge.ml_registry.services.registry_aliases import adjudicate_run
from knowledge.ml_registry.services.registry_runs import complete_run
from knowledge.ml_registry.storage import Registry


FIXTURES = Path(__file__).parent / "fixtures" / "policy_gate"


def _fixture(campaign_id: str) -> tuple[dict[str, object], dict[str, list[dict[str, object]]]]:
    spec = json.loads((FIXTURES / "campaign.json").read_text())
    spec["campaign_id"] = campaign_id
    rows = [json.loads(line) for line in (FIXTURES / "scoring.jsonl").read_text().splitlines()]
    return spec, {"fixture_scoring": rows}


def _measured(dispatch: CampaignDispatch) -> CampaignOutcomeRecord:
    return CampaignOutcomeRecord(
        schema_version=CampaignOutcomeRecord.VERSION,
        campaign_id=dispatch.campaign.campaign_id,
        outcome=CampaignOutcome.MEASURED,
        reason="fixture campaign exhausted its useful arms",
        attempt=1,
    )


def test_fixture_portfolio_reports_refusal_and_runs_every_other_campaign(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path)
    for campaign_id in ("one", "two", "three"):
        spec, corpora = _fixture(campaign_id)
        assert register_campaign_for_run(registry, spec, scoring_corpora=corpora)

    refused, corpora = _fixture("refused")
    refused["rope"] = {"value": 999}
    assert not register_campaign_for_run(registry, refused, scoring_corpora=corpora)

    calls: list[str] = []

    def drive(dispatch: CampaignDispatch) -> CampaignOutcomeRecord:
        calls.append(dispatch.campaign.campaign_id)
        return _measured(dispatch)

    report = run_registered_campaigns(registry, drive, max_active=2)

    assert sorted(calls) == ["one", "three", "two"]
    assert [entry.campaign_id for entry in report.outcomes] == ["one", "two", "three", "refused"]
    refusal = report.outcomes[-1]
    assert refusal.outcome is CampaignOutcome.REFUTED
    assert "rope is registration-derived" in refusal.reason
    with sqlite3.connect(registry.db_path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_corrected_registration_supersedes_its_recorded_refusal(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    spec, corpora = _fixture("corrected")
    invalid = dict(spec)
    invalid["rope"] = {"value": 999}
    assert not register_campaign_for_run(registry, invalid, scoring_corpora=corpora)
    assert register_campaign_for_run(registry, spec, scoring_corpora=corpora)

    report = run_registered_campaigns(registry, _measured)

    assert report.outcomes[0].outcome is CampaignOutcome.MEASURED


def test_restart_redispatches_unanswered_claim_and_never_duplicates_a_verdict(
    tmp_path: Path,
) -> None:
    spec, corpora = _fixture("resume-me")
    registry = Registry(tmp_path)
    assert register_campaign_for_run(registry, spec, scoring_corpora=corpora)
    registry.create_experiment(
        experiment_id="resume-me",
        spec_digest="a" * 64,
        stages=["survey"],
        metric="fixture_score",
        direction="maximize",
        win_condition={"metric_at_least": 1.0},
        rope=0.01,
        baseline_throughput=1.0,
    )
    _create_run(registry, "answered-arm", "first-candidate")
    complete_run(registry, run_id="answered-arm", metrics={
        "metric": 0.5,
        "validity": "valid",
        "throughput": 2.0,
        "throughput_unit": "samples_per_second",
        "memory_gb": 0.1,
        "cpu_time": 1.0,
        "load": {"start_1m": 0.0, "end_1m": 0.0},
    })
    adjudicate_run(
        registry,
        run_id="answered-arm",
        verdict="rejected",
        status="succeeded",
        reason="fixture verdict",
    )
    _create_run(registry, "unanswered-arm", "candidate")

    seen: list[CampaignDispatch] = []

    def killed(dispatch: CampaignDispatch) -> CampaignOutcomeRecord:
        seen.append(dispatch)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_registered_campaigns(registry, killed)

    restarted = Registry(tmp_path)

    def resumed(dispatch: CampaignDispatch) -> CampaignOutcomeRecord:
        seen.append(dispatch)
        return _measured(dispatch)

    first_report = run_registered_campaigns(restarted, resumed)
    assert seen[-1].last_adjudicated_run_id == "answered-arm"
    assert seen[-1].redispatch_run_ids == ("unanswered-arm",)
    assert len(first_report.outcomes) == 1

    def duplicate(_: CampaignDispatch) -> CampaignOutcomeRecord:
        pytest.fail("a campaign with a committed terminal outcome was dispatched again")

    second_report = run_registered_campaigns(Registry(tmp_path), duplicate)
    assert second_report == first_report
    outcome_events = [
        event
        for event in Registry(tmp_path).list_events()
        if event.event_type == "campaign_outcome_recorded"
    ]
    assert len(outcome_events) == 1


def _create_run(registry: Registry, run_id: str, idea_id: str) -> None:
    registry.create_run(
        run_id=run_id,
        experiment_id="resume-me",
        idea_id=idea_id,
        stage="survey",
        family="fixture",
        params={},
        metrics={},
        code_ref={
            "schema_version": 1,
            "repo": str(Path(__file__).resolve().parents[3]),
            "sha": _head(),
            "base_sha": _head(),
            "diff_hash": "0" * 64,
            "diff_lines": 0,
        },
        device_fingerprint="cpu:fixture",
        status="running",
        verdict=None,
        started_at=1.0,
        finished_at=None,
        claim_owner="killed-worker",
        heartbeat_at=1.0,
    )


def _head() -> str:
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[3],
    ).stdout.strip()
