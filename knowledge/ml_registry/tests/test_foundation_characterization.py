"""First-ticket pins for known lifecycle gaps; strict xfails must become fixes in later phases."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from knowledge.ml_registry.controller import PortfolioController, PollResult
from knowledge.ml_registry.portfolio import CampaignStatus, Portfolio
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.scheduler import PortfolioError, schedule
from knowledge.ml_registry.write_path import RegistrySpace, register_idea, register_model, register_trial


CAPACITY = {"cpus": 2, "ram_gb": 4}


def _spec(cid, **extra):
    return {"id": cid, "command": ["true"], "resources": {"cpus": 1}, **extra}


class Backend:
    def __init__(self):
        self.results = {}
    def submit(self, job):
        self.results.setdefault(job.campaign_id, PollResult("running"))
        return job.campaign_id
    def poll(self, job_id):
        return self.results[job_id]


def _ready(portfolio, cid):
    campaign = portfolio.add_campaign(cid, cid)
    campaign.status = CampaignStatus.READY


def test_fixture_a_two_root_campaigns_occupy_two_slots(tmp_path):
    portfolio = Portfolio()
    _ready(portfolio, "gpu-root")
    _ready(portfolio, "cpu-root")
    controller = PortfolioController(portfolio=portfolio,
        campaign_specs=[_spec("gpu-root"), _spec("cpu-root")], capacity=CAPACITY,
        backend=Backend(), state_path=tmp_path / "controller.json")
    assert set(controller.tick().started) == {"gpu-root", "cpu-root"}


def test_fixture_b_a_campaign_cannot_run_two_trials_for_one_idea():
    space = RegistrySpace()
    model_id = register_model(space, {
        "metric": "f1", "direction": "maximize", "win_condition": {"metric_at_least": .9},
        "baseline": "base", "noise_floor": .01, "baseline_throughput": 1.0,
        "diff_size_limit": 8, "max_trials": 5, "max_discovered_ideas": 0,
    })
    idea_id = register_idea(space, {"model_id": model_id, "origin": "seeded",
                                    "axis": "representation", "description": "fixture arm"})
    first = {"model_id": model_id, "idea_id": idea_id, "commit": "base", "status": "complete",
             "throughput": 1.0, "diff_lines": 1}
    register_trial(space, first, frozenset({"base", "candidate"}))
    with pytest.raises(RegistryValidationError, match="in flight"):
        register_trial(space, {**first, "commit": "candidate"}, frozenset({"base", "candidate"}))


@pytest.mark.xfail(
    strict=True,
    reason="fixture D: process exit is not reverified against a production ModelVersion alias",
)
@pytest.mark.parametrize(
    ("completion_claim", "reason"),
    [
        (None, "outcome"),
        ({"outcome": "COMPLETE", "model_id": "model-root"}, "production alias"),
        ({
            "outcome": "COMPLETE", "model_id": "model-root", "version": 1,
            "alias": "production", "artifact_checksum": "wrong",
        }, "checksum"),
        ({
            "outcome": "COMPLETE", "model_id": "model-root", "version": 1,
            "alias": "production", "artifact_checksum": "a" * 64,
            "lineage_status": "superseded",
        }, "lineage"),
    ],
    ids=("no-outcome", "no-production-alias", "checksum-mismatch", "superseded-lineage"),
)
def test_fixture_d_exit_zero_without_verified_production_alias_is_failed(
    tmp_path, completion_claim, reason,
):
    portfolio = Portfolio()
    _ready(portfolio, "root")
    backend = Backend()
    controller = PortfolioController(portfolio=portfolio, campaign_specs=[_spec("root")], capacity=CAPACITY,
                                     backend=backend, state_path=tmp_path / "controller.json")
    controller.tick()
    backend.results["root"] = PollResult("completed", artifact=completion_claim)
    controller.tick()
    assert controller.records["root"].state == "failed"
    assert reason in (controller.records["root"].message or "").lower()


@pytest.mark.xfail(strict=True, reason="P-4: scheduler still accepts loose campaign depends_on edges")
def test_p4_scheduler_rejects_depends_on_key():
    with pytest.raises(PortfolioError, match="depends_on"):
        schedule([_spec("root", depends_on=[])], {}, CAPACITY, max_concurrency=1)


@pytest.mark.xfail(strict=True, reason="fixture E: restart does not reconcile a persisted launch intent")
def test_fixture_e_restart_reads_launch_intent_before_retry(tmp_path):
    state = tmp_path / "controller.json"
    state.write_text(json.dumps({"records": {"root": {
        "backend_job_id": "root.attempt-1", "state": "dispatching", "attempt": 1,
        "next_retry_at": 0.0, "message": None, "started_at": 1.0, "checkpoint_uri": None,
    }}}))
    dispatch = tmp_path / "dispatch"
    dispatch.mkdir()
    (dispatch / "root.attempt-1.process.json").write_text(json.dumps({"pid": 1, "pgid": 1,
        "intent_id": "root-1", "spec_digest": "a" * 64, "registry_trial_id": None}))
    portfolio = Portfolio()
    _ready(portfolio, "root")
    controller = PortfolioController(portfolio=portfolio, campaign_specs=[_spec("root")], capacity=CAPACITY,
                                     backend=Backend(), state_path=state)
    assert controller.records["root"].state == "running"


@pytest.mark.xfail(strict=True, reason="fixture F: controller backend owns no process-group cancellation API")
def test_fixture_f_backend_exposes_force_cancel_and_drain():
    backend = Backend()
    assert callable(backend.cancel)
    assert callable(backend.drain)


@pytest.mark.xfail(
    strict=True,
    reason="fixture O: the canonical SQLite registry schema and write guards do not exist",
)
def test_fixture_o_sqlite_registry_has_exact_canonical_tables_and_guards(tmp_path):
    registry_module = importlib.import_module("knowledge.ml_registry.storage.registry")
    registry = registry_module.RegistryStore(tmp_path / "registry")

    assert registry.table_names() == {
        "experiments", "runs", "artifacts", "registered_models", "model_versions",
        "lineage", "aliases", "events",
    }
    assert registry.is_single_writer
    assert registry.model_versions_are_immutable
    assert registry.alias_writer("champion") == "adjudication"
    assert registry.alias_writer("production") == "finalize"


@pytest.mark.xfail(
    strict=True,
    reason="fixture O: results.tsv is not yet a byte-stable export of canonical runs",
)
def test_fixture_o_results_tsv_is_a_runs_export_not_an_input(tmp_path):
    registry_module = importlib.import_module("knowledge.ml_registry.storage.registry")
    export_module = importlib.import_module("knowledge.ml_registry.contracts.runs_export")
    registry = registry_module.RegistryStore(tmp_path / "registry")

    run_id = registry.insert_fixture_run(code_ref={
        "repo": str(tmp_path), "sha": "a" * 40, "base_sha": "b" * 40,
        "diff_hash": "c" * 64, "diff_lines": 3,
    })
    payload = export_module.RunsExport.from_registry(registry).serialize()

    assert run_id.encode() in payload
    assert registry.imports_results_tsv is False


@pytest.mark.xfail(
    strict=True,
    reason="public CLI help still exposes superseded artifact/promotion/convergence vocabulary",
)
def test_public_cli_help_uses_standard_registry_vocabulary():
    outputs = []
    for module in (
        "knowledge.ml_registry.cli",
        "knowledge.ml_registry.controller_cli",
        "knowledge.ml_registry.portfolio_cli",
    ):
        result = subprocess.run(
            [sys.executable, "-m", module, "--help"],
            cwd=Path(__file__).resolve().parents[3],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0
        outputs.append(f"{result.stdout}\n{result.stderr}")
    help_text = "\n".join(outputs).lower().replace("-", "")

    assert all(forbidden not in help_text for forbidden in (
        "promotionrecord", "campaignartifact", "convergence_run", "artifactstore",
    ))
    assert "run" in help_text
    assert "registered model" in help_text
    assert "model version" in help_text
    assert "alias" in help_text
