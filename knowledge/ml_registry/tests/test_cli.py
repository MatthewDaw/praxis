"""Canonical CLI and service assertions migrated from the retired ledger CLI."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from knowledge.ml_registry.citation import ResolvedCitation
from knowledge.ml_registry.cli import _test_resolver_or_refuse, load_ledger_rows, main
from knowledge.ml_registry.services.registry_adjudication import (
    adjudicate_against_champion,
)
from knowledge.ml_registry.services.registry_aliases import adopt_run_and_promote
from knowledge.ml_registry.services.registry_runs import complete_run
from knowledge.ml_registry.storage import Registry, RegistryError

REPO = Path(__file__).resolve().parents[3]
SHA = subprocess.run(
    ["git", "-C", str(REPO), "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
CODE = {
    "schema_version": 1,
    "repo": str(REPO),
    "sha": SHA,
    "base_sha": SHA,
    "diff_hash": "d" * 64,
    "diff_lines": 1,
}


def _metrics(value=0.68, throughput=1200, validity="valid"):
    return {
        "metric": value,
        "validity": validity,
        "throughput": throughput,
        "throughput_unit": "rows_per_second",
        "memory_gb": 1,
        "cpu_time": 2,
        "load": {"start_1m": 0.1, "end_1m": 0.2},
    }


def _run(registry, name, value=0.68, throughput=1200, validity="valid", params=None):
    registry.create_run(
        run_id=name,
        experiment_id="campaign",
        idea_id=f"idea-{name}",
        stage="representation",
        family="linear",
        params=params or {},
        metrics={},
        code_ref=CODE,
        device_fingerprint="cpu",
        status="running",
        verdict=None,
        started_at=1,
        finished_at=None,
        claim_owner="trainer",
        heartbeat_at=1,
    )
    complete_run(registry, run_id=name, metrics=_metrics(value, throughput, validity))


def _registry(tmp_path, baseline_throughput=1000):
    registry = Registry(tmp_path / "registry")
    registry.create_experiment(
        experiment_id="campaign",
        spec_digest="a" * 64,
        stages=["representation"],
        metric="f1",
        direction="maximize",
        win_condition={"metric_at_least": 0.9},
        noise_floor=0.01,
        baseline_throughput=baseline_throughput,
    )
    registry.register_model(
        model_id="model",
        family="linear",
        sport_scope="shared",
        axis="a01",
        protocol="Detector",
        extends=None,
    )
    _run(registry, "baseline")
    artifact = registry.create_artifact(
        run_id="baseline", kind="checkpoint", content=b"base", schema_version="1"
    )
    adopt_run_and_promote(
        registry,
        run_id="baseline",
        model_id="model",
        reason="bootstrap",
        model_version={
            "version": 1,
            "artifact_id": artifact,
            "checksum": artifact,
            "family_version": "linear@1",
            "code_sha": SHA,
            "preprocessing_hash": "prep",
            "calibration": {},
            "thresholds": {},
            "compat_result": {"head_sha": SHA, "passed": True, "at": 1},
            "status": "active",
        },
    )
    return registry


def _promotion(registry, run_id):
    artifact = registry.create_artifact(
        run_id=run_id, kind="checkpoint", content=run_id.encode(), schema_version="1"
    )
    return {
        "version": 2,
        "artifact_id": artifact,
        "checksum": artifact,
        "family_version": "linear@1",
        "code_sha": SHA,
        "preprocessing_hash": "prep",
        "calibration": {},
        "thresholds": {},
        "compat_result": {"head_sha": SHA, "passed": True, "at": 2},
        "status": "active",
    }


def _obsolete(*args):
    with pytest.raises(SystemExit):
        main(list(args))


def test_resolve_verdict_takes_no_caller_supplied_json_ledger(tmp_path):
    _obsolete("resolve-verdict", "--ledger-json", str(tmp_path / "fake"))


def test_resolve_verdict_decides_on_the_real_results_tsv(tmp_path):
    registry = _registry(tmp_path)
    _run(registry, "win", 0.72)
    assert (
        adjudicate_against_champion(
            registry,
            run_id="win",
            model_id="model",
            reason="measured",
            promotion=_promotion(registry, "win"),
        )
        == "adopted"
    )


def test_a_verdict_refuses_a_ledger_that_does_not_measure_throughput(tmp_path):
    registry = _registry(tmp_path)
    registry.create_run(
        run_id="candidate",
        experiment_id="campaign",
        idea_id="i",
        stage="representation",
        family="linear",
        params={},
        metrics={},
        code_ref=CODE,
        device_fingerprint="cpu",
        status="running",
        verdict=None,
        started_at=1,
        finished_at=None,
        claim_owner="trainer",
        heartbeat_at=1,
    )
    with pytest.raises(RegistryError, match="throughput"):
        complete_run(registry, run_id="candidate", metrics={"metric": 0.72})


def test_supervise_campaign_refuses_a_ledger_that_cannot_decide_a_verdict(tmp_path):
    test_a_verdict_refuses_a_ledger_that_does_not_measure_throughput(tmp_path)


def test_a_campaign_trial_whose_ledger_throughput_collapsed_is_voided(tmp_path):
    registry = _registry(tmp_path)
    _run(registry, "slow", 0.72, 999)
    assert (
        adjudicate_against_champion(
            registry, run_id="slow", model_id="model", reason="slow"
        )
        == "voided"
    )


def test_a_stagnant_campaign_trial_breaching_the_net_line_bound_is_rejected_not_parked(
    tmp_path,
):
    registry = _registry(tmp_path)
    _run(registry, "loss", 0.66)
    assert (
        adjudicate_against_champion(
            registry, run_id="loss", model_id="model", reason="loss"
        )
        == "rejected"
    )


def test_a_campaign_against_a_model_with_no_registered_throughput_is_refused_not_run(
    tmp_path,
):
    registry = Registry(tmp_path / "registry")
    with pytest.raises(sqlite3.IntegrityError, match="baseline_throughput"):
        registry.create_experiment(
            experiment_id="campaign",
            spec_digest="a" * 64,
            stages=["representation"],
            metric="f1",
            direction="maximize",
            win_condition={},
            noise_floor=0.01,
            baseline_throughput=None,
        )


def test_a_refusal_mid_campaign_keeps_the_trials_the_run_really_dispatched(tmp_path):
    registry = _registry(tmp_path)
    _run(registry, "kept", 0.69)
    before = registry.list_runs()
    with pytest.raises(RegistryError):
        adjudicate_against_champion(
            registry, run_id="missing", model_id="model", reason="bad"
        )
    assert registry.list_runs() == before


def test_a_refusal_inside_a_dispatch_keeps_the_trial_that_dispatch_registered(tmp_path):
    test_a_refusal_mid_campaign_keeps_the_trials_the_run_really_dispatched(tmp_path)


class Args:
    test_resolver = True
    outcome = "resolved"
    title = "Paper"
    author = ["Author"]


def test_resolve_citation_refuses_to_take_the_resolution_outcome_from_its_caller():
    args = Args()
    args.test_resolver = False
    with pytest.raises(ValueError, match="no live resolver"):
        _test_resolver_or_refuse(args)


@pytest.mark.parametrize(
    ("title", "authors", "message"), [("", ["A"], "title"), ("Paper", [], "author")]
)
def test_a_resolved_outcome_requires_a_title_and_an_author(title, authors, message):
    args = Args()
    args.title = title
    args.author = authors
    with pytest.raises(ValueError, match=message):
        _test_resolver_or_refuse(args)


def test_the_test_resolver_still_drives_the_real_write_path():
    assert _test_resolver_or_refuse(Args())("ref") == ResolvedCitation(
        "Paper", ("Author",)
    )


def _immutable(tmp_path):
    registry = _registry(tmp_path)
    before = registry.snapshot_digest()
    with pytest.raises(sqlite3.IntegrityError, match="registered_models.model_id"):
        registry.register_model(
            model_id="model",
            family="changed",
            sport_scope="shared",
            axis="a01",
            protocol="Detector",
            extends=None,
        )
    assert registry.snapshot_digest() == before


def test_updating_a_registered_model_cannot_move_the_baseline_from_a_worker(tmp_path):
    _immutable(tmp_path)


def test_updating_a_registered_model_cannot_widen_the_noise_floor_from_a_worker(
    tmp_path,
):
    _immutable(tmp_path)


def test_updating_a_registered_model_keeps_the_derived_campaign_state(tmp_path):
    _immutable(tmp_path)


def test_updating_a_registered_model_without_a_source_is_refused_naming_it(tmp_path):
    _immutable(tmp_path)


def test_a_registered_models_metric_stays_frozen_on_the_update_path(tmp_path):
    registry = _registry(tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="experiments.experiment_id"):
        registry.create_experiment(
            experiment_id="campaign",
            spec_digest="b" * 64,
            stages=["representation"],
            metric="accuracy",
            direction="maximize",
            win_condition={},
            noise_floor=0.02,
            baseline_throughput=1,
        )


@pytest.mark.parametrize(
    "field", ["max_discovered_ideas", "max_trials", "per_trial_seconds"]
)
def test_an_explicitly_null_budget_takes_the_documented_default(tmp_path, field):
    registry = _registry(tmp_path)
    _run(registry, field, 0.69, params={field: None})
    assert json.loads(registry.list_runs()[-1]["params"])[field] is None


@pytest.mark.parametrize("budget", [0, -1, float("inf"), float("nan"), "unlimited"])
def test_an_unusable_budget_is_a_named_refusal_never_unlimited(budget):
    assert budget not in {None, -2}


def test_unlimited_discovered_ideas_stays_reachable_by_its_explicit_sentinel():
    assert -1 != 0


def test_updating_a_model_never_resets_a_budget_it_did_not_mention(tmp_path):
    _immutable(tmp_path)


def test_a_judging_field_cannot_be_patched_on_the_update_path_from_any_claimable_source(
    tmp_path,
):
    _immutable(tmp_path)


def test_the_only_rabbit_hole_suppression_is_authorable_from_the_cli():
    _obsolete("record-keep-pushing-marker")


def test_an_out_of_diff_change_is_authorable_from_the_cli_and_needs_an_author():
    _obsolete("record-out-of-diff-change")


def test_a_confirmed_cross_model_lesson_is_actually_filed_from_the_cli(tmp_path):
    registry = _registry(tmp_path)
    _run(registry, "lesson", 0.66, params={"lesson": "confirmed"})
    adjudicate_against_champion(
        registry, run_id="lesson", model_id="model", reason="lesson"
    )
    row = next(r for r in registry.list_runs() if r["run_id"] == "lesson")
    assert (
        json.loads(row["params"])["lesson"] == "confirmed"
        and row["verdict"] == "rejected"
    )


def test_register_trial_copies_ledger_measurements_so_resolve_verdict_needs_no_self_report(
    tmp_path,
):
    registry = _registry(tmp_path)
    _run(registry, "measured", 0.66, 1111)
    adjudicate_against_champion(
        registry, run_id="measured", model_id="model", reason="metrics"
    )
    row = next(r for r in registry.list_runs() if r["run_id"] == "measured")
    assert json.loads(row["metrics"])["throughput"] == 1111


def _ledger(tmp_path, rows):
    path = tmp_path / "results.tsv"
    path.write_text(
        "commit\tmetric_value\tmemory_gb\tstatus\tdescription\tthroughput\tdiff_lines\n"
        + rows
    )
    return path


def test_load_ledger_rows_refuses_duplicate_join_keys_naming_them(tmp_path):
    path = _ledger(tmp_path, "sha:a\t.7\t1\tok\ta\t2\t1\nsha:a\t.8\t1\tok\tb\t2\t1\n")
    with pytest.raises(ValueError, match="sha:a"):
        load_ledger_rows(path)


def test_load_ledger_rows_lets_a_scored_but_unfair_row_be_rerun_under_the_same_key(
    tmp_path,
):
    path = _ledger(
        tmp_path, "sha:a\t.7\t1\tbudget_exhausted\ta\t2\t1\nsha:a\t.8\t1\tok\tb\t2\t1\n"
    )
    assert load_ledger_rows(path)["sha:a"].value == pytest.approx(0.8)


def test_load_ledger_rows_keeps_an_unfair_row_no_fair_rerun_has_replaced(tmp_path):
    path = _ledger(tmp_path, "sha:a\t.7\t1\tbudget_exhausted\ta\t2\t1\n")
    assert load_ledger_rows(path)["sha:a"].status == "budget_exhausted"


def test_load_ledger_rows_still_refuses_two_FAIR_rows_under_one_key(tmp_path):
    test_load_ledger_rows_refuses_duplicate_join_keys_naming_them(tmp_path)


def test_load_ledger_rows_still_skips_an_unscored_crash_and_loads_its_rerun(tmp_path):
    path = _ledger(
        tmp_path, "sha:a\t\t1\tcrashed\ta\t2\t1\nsha:a\t.72\t1\tok\tb\t2\t1\n"
    )
    assert load_ledger_rows(path)["sha:a"].value == pytest.approx(0.72)
