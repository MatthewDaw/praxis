"""First-ticket pins for known lifecycle gaps; strict xfails must become fixes in later phases."""

from __future__ import annotations

import importlib
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


@pytest.mark.parametrize(
    ("claim", "reason"),
    [
        ("missing_outcome", "outcome"),
        ("missing_production_alias", "production alias"),
        ("checksum_mismatch", "checksum"),
        ("superseded_lineage", "superseded lineage"),
    ],
    ids=("no-outcome", "no-production-alias", "checksum-mismatch", "superseded-lineage"),
)
def test_fixture_d_exit_zero_without_verified_production_alias_is_failed(
    tmp_path, claim, reason,
):
    fixtures = importlib.import_module("knowledge.ml_registry.testing.portfolio_fixtures")
    scenario = fixtures.invalid_completion_scenario(tmp_path, claim=claim)
    scenario.finish_root_and_poll()
    assert scenario.record("R1").state == "failed"
    assert reason in (scenario.record("R1").message or "").lower()


def test_p4_scheduler_rejects_depends_on_key():
    with pytest.raises(PortfolioError, match="depends_on"):
        schedule([_spec("root", depends_on=[])], {}, CAPACITY, max_concurrency=1)


def test_fixture_e_restart_reads_launch_intent_before_retry(tmp_path):
    fixtures = importlib.import_module("knowledge.ml_registry.testing.portfolio_fixtures")
    scenario = fixtures.restart_scenario(tmp_path, crash_after="intent_written")
    scenario.run_until_crash()
    scenario.restart_and_reconcile()
    assert scenario.live_process_group_count("R1") <= 1
    assert scenario.trial_count("R1", idea_id="idea-1") == 1


def test_fixture_f_backend_exposes_reconcile_and_cancel_while_controller_owns_drain(tmp_path):
    controller_module = importlib.import_module("knowledge.ml_registry.controller")
    backend = controller_module.ExecutorProcessBackend(tmp_path / "dispatch")
    assert callable(backend.reconcile)
    assert callable(backend.cancel)
    assert callable(controller_module.PortfolioController.stop)


def test_fixture_o_sqlite_registry_has_exact_canonical_tables_and_guards(tmp_path):
    registry_module = importlib.import_module("knowledge.ml_registry.storage.registry")
    registry = registry_module.Registry(tmp_path / "registry")

    assert set(registry.table_names()) == {
        "experiments", "runs", "artifacts", "registered_models", "model_versions",
        "lineage", "aliases", "events",
    }
    assert registry.is_single_writer
    assert registry.model_versions_are_immutable
    assert registry.alias_writer("champion") == "adjudication"
    assert registry.alias_writer("production") == "finalize"


def test_fixture_o_results_tsv_is_a_runs_export_not_an_input(tmp_path):
    from knowledge.ml_registry import HistoricalLedgerImporter, Registry, RunsExport
    registry = Registry(tmp_path / "registry")
    payload = (
        b"commit\tmetric_value\tmemory_gb\tstatus\tdescription\tthroughput\tdiff_lines\r\n"
        b"abc1234:arm\t.8\t1\tok\tarm\t2\t3\r\n"
    )
    HistoricalLedgerImporter(registry).import_ledger(
        payload, experiment_id="fixture", spec_digest="d" * 64,
        metric="score", direction="maximize",
    )
    assert RunsExport(registry).render(experiment_id="fixture") == payload
    assert registry.imports_results_tsv is False


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
        "promotion" + "record", "campaign" + "artifact", "convergence" + "_run",
        "artifact" + "store",
    ))
    assert "run" in help_text
    assert "registered model" in help_text
    assert "model version" in help_text
    assert "alias" in help_text
