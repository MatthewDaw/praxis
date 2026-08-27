"""The vector judge: a model shipping several outputs is judged on a metric VECTOR,
and adoption is Pareto -- at least one judged metric wins, no judged metric regresses
beyond its rope, and a trade (a gain bought with a regression) is REJECTED whatever the
gain. Single-metric campaigns are untouched; a single-entry ``metrics`` list IS the
scalar judge."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from knowledge.ml_registry import Registry
from knowledge.ml_registry.contracts import CampaignSpec, ContractError
from knowledge.ml_registry.services.registry_adjudication import adjudicate_against_champion
from knowledge.ml_registry.services.registry_aliases import adopt_run_and_promote
from knowledge.ml_registry.services.registry_runs import complete_run
from knowledge.ml_registry.storage import RegistryError


REPO = Path(__file__).resolve().parents[3]
SHA = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], check=True,
                     capture_output=True, text=True).stdout.strip()
DIFF = "d" * 64

CHAMPION_UNITS = {"ap50": [.55, .60, .65, .60], "idf1": [.68, .70, .72, .70]}
CHAMPION_METRICS = {"ap50": .60, "idf1": .70}


def metric_entry(name: str, **overrides: object) -> dict[str, object]:
    return {
        "name": name,
        "direction": "maximize",
        "operating_point": {"selection": "frozen", "threshold": .5},
        "aggregation": [{"level": "unit", "unit": "unit_id", "minimum_sample": 2}],
        "scoring_corpus": "fixture",
        "split_unit": "unit_id",
        "adjudication": {
            "method": "paired_bootstrap_percentile",
            "resamples": 500,
            "confidence_level": .95,
            "seed": 17,
            "aggregation": "mean",
        },
        **overrides,
    }


def spec_mapping(**judge: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "campaign_id": "campaign",
        "model_id_policy": "existing_registered_model",
        "axis": "a01",
        "sport_scope": ["shared"],
        "target_ontology": "fixture",
        **judge,
        "stages": [{"name": "representation"}],
        "corpora": [{"id": "fixture", "roles": ["scoring"], "split_unit": "unit_id"}],
        "requires": [],
        "produces": [{"artifact_type": "checkpoint", "schema_version": "1"}],
        "supervision": {"mode": "composing"},
        "resources": {"lane": "cpu"},
        "isolation": {"state_root": "state/campaign"},
        "production": {"protocol": "Detector"},
        "extends": [],
        "deterministic_incumbent": None,
        "learned_escalation": False,
        "rope": None,
    }


SCORING_CORPORA = {"fixture": [
    {"unit_id": "one", "ap50": .59, "idf1": .69},
    {"unit_id": "two", "ap50": .61, "idf1": .71},
]}


def run_metrics(value: object) -> dict[str, object]:
    return {"metric": value, "validity": "valid", "throughput": 3.5,
            "throughput_unit": "rows_per_second", "memory_gb": 1.25, "cpu_time": 8.0,
            "load": {"start_1m": 0.2, "end_1m": 0.4}}


def create_run(registry: Registry, run_id: str, value: object) -> None:
    registry.create_run(
        run_id=run_id, experiment_id="campaign", idea_id=f"idea-{run_id}",
        stage="representation", family="linear", params={},
        metrics={}, code_ref={"schema_version": 1, "repo": str(REPO), "sha": SHA,
                              "base_sha": SHA, "diff_hash": DIFF, "diff_lines": 1},
        device_fingerprint="cpu:test", status="running", verdict=None, started_at=1,
        finished_at=None, claim_owner="trainer", heartbeat_at=1,
    )
    complete_run(registry, run_id=run_id, metrics=run_metrics(value))


def vector_registry(tmp_path: Path) -> Registry:
    registry = Registry(tmp_path)
    registry.create_experiment(experiment_id="campaign", spec_digest="a" * 64,
        stages=["representation"], metric="vector", direction="maximize",
        win_condition={"metric_at_least": 0.9}, rope=0.01, baseline_throughput=3.3)
    registry.register_model(model_id="model", family="linear", sport_scope="shared",
                            axis="a01", protocol="Detector", extends=None)
    registry.register_campaign_spec(
        spec_mapping(metrics=[metric_entry("ap50"), metric_entry("idf1")]),
        scoring_corpora=SCORING_CORPORA,
    )
    create_run(registry, "baseline", CHAMPION_METRICS)
    artifact = registry.create_artifact(run_id="baseline", kind="checkpoint",
                                        content=b"base", schema_version="1")
    adopt_run_and_promote(registry, run_id="baseline", model_id="model", reason="bootstrap",
        model_version={"version": 1, "artifact_id": artifact, "checksum": artifact,
        "family_version": "linear@1", "code_sha": SHA, "preprocessing_hash": "prep",
        "calibration": {}, "thresholds": {},
        "compat_result": {"head_sha": SHA, "passed": True, "at": 1}, "status": "active"})
    return registry


def promotion(registry: Registry, run_id: str, version: int = 2) -> dict[str, object]:
    artifact = registry.create_artifact(run_id=run_id, kind="checkpoint",
                                        content=f"winner:{run_id}".encode(), schema_version="1")
    return {"version": version, "artifact_id": artifact, "checksum": artifact,
            "family_version": "linear@1", "code_sha": SHA, "preprocessing_hash": "prep",
            "calibration": {}, "thresholds": {},
            "compat_result": {"head_sha": SHA, "passed": True, "at": 2}, "status": "active"}


def vector_evidence(candidate_units: dict[str, list[float]]) -> dict[str, object]:
    names = sorted(candidate_units)
    return {
        "candidate_run_id": "candidate",
        "champion_run_id": "baseline",
        "baseline_run_id": "baseline",
        "resamples": 500,
        "confidence_level": .95,
        "seed": 17,
        "units": [
            {"unit_id": f"unit-{index}",
             "candidate": {name: candidate_units[name][index] for name in names},
             "champion": {name: CHAMPION_UNITS[name][index] for name in names}}
            for index in range(4)
        ],
    }


def aggregates(candidate_units: dict[str, list[float]]) -> dict[str, float]:
    return {name: round(sum(values) / len(values), 12)
            for name, values in candidate_units.items()}


def shifted(name_deltas: dict[str, float]) -> dict[str, list[float]]:
    return {name: [round(value + name_deltas[name], 12) for value in values]
            for name, values in CHAMPION_UNITS.items()}


def adjudicate(registry: Registry, candidate_units: dict[str, list[float]], *,
               adopt: bool, metrics_override: object = None) -> str:
    create_run(registry, "candidate",
               aggregates(candidate_units) if metrics_override is None else metrics_override)
    return adjudicate_against_champion(
        registry, run_id="candidate", model_id="model", reason="vector comparison",
        promotion=promotion(registry, "candidate") if adopt else None,
        paired_evidence=vector_evidence(candidate_units),
    )


def _candidate_event(registry: Registry) -> dict:
    return next(
        event.payload for event in reversed(registry.list_events())
        if event.event_type in {"run_adopted", "run_adjudicated"}
        and event.payload["run_id"] == "candidate"
    )


# ---------------------------------------------------------------------------
# Spec shape
# ---------------------------------------------------------------------------

def test_spec_with_both_metric_and_metrics_is_refused_by_name() -> None:
    with pytest.raises(ContractError, match="exactly one of 'metric'.*got both"):
        CampaignSpec.from_mapping(spec_mapping(metric=metric_entry("f1"),
                                               metrics=[metric_entry("ap50")]))


def test_spec_with_neither_metric_nor_metrics_is_refused_by_name() -> None:
    with pytest.raises(ContractError, match="exactly one of 'metric'.*got neither"):
        CampaignSpec.from_mapping(spec_mapping())


def test_single_entry_metrics_list_is_the_scalar_judge() -> None:
    scalar = CampaignSpec.from_mapping(spec_mapping(metric=metric_entry("f1")))
    one_entry = CampaignSpec.from_mapping(spec_mapping(metrics=[metric_entry("f1")]))
    assert one_entry.metric == scalar.metric
    assert one_entry.metrics == ()
    assert one_entry.to_mapping() == scalar.to_mapping()


def test_duplicate_judged_metric_names_are_refused() -> None:
    with pytest.raises(ContractError, match="once.*ap50|duplicated: ap50"):
        CampaignSpec.from_mapping(spec_mapping(metrics=[metric_entry("ap50"),
                                                        metric_entry("ap50")]))


def test_vector_registration_computes_one_rope_per_judged_metric(tmp_path: Path) -> None:
    registry = vector_registry(tmp_path)
    spec_event = next(event for event in reversed(registry.list_events())
                      if event.event_type == "campaign_spec_registered")
    rope = spec_event.payload["rope"]
    assert rope["method"] == "vector"
    assert set(rope["metrics"]) == {"ap50", "idf1"}
    for name in ("ap50", "idf1"):
        assert rope["metrics"][name]["method"] == "split_unit_bootstrap"
        assert rope["metrics"][name]["metric"] == name


def test_vector_units_may_carry_identity_fields_another_metric_needs(tmp_path: Path) -> None:
    """One unit list, two aggregations: extra identity fields are not a refusal.

    Mapping scores macro-by-sport (needs ``stratum``); paint scores a nested
    truth-kind/corpus macro (needs ``truth_kind`` and ``corpus``). The vector
    judge carries both on every unit. Extra keys must be ignored, not scored
    and not refused -- a missing *required* key is still a refusal.
    """
    from knowledge.ml_registry.services.paired_adjudication import paired_interval

    registry = vector_registry(tmp_path)
    evidence = vector_evidence(shifted({"ap50": .03, "idf1": 0.0}))
    for unit in evidence["units"]:
        unit["stratum"] = "basketball"
        unit["truth_kind"] = "derived_calibration"
        unit["corpus"] = "deepsport-basketball-instants"
    create_run(registry, "candidate", aggregates(shifted({"ap50": .03, "idf1": 0.0})))
    verdict = adjudicate_against_champion(
        registry, run_id="candidate", model_id="model", reason="extra identity fields",
        promotion=promotion(registry, "candidate"),
        paired_evidence=evidence,
    )
    assert verdict == "adopted"

    policy = metric_entry("ap50")["adjudication"]
    projected = {
        "candidate_run_id": "candidate",
        "champion_run_id": "baseline",
        "resamples": 500,
        "confidence_level": .95,
        "seed": 17,
        "units": [
            {"champion": .5, "candidate": .6, "stratum": "x"},
            {"champion": .4, "candidate": .7, "stratum": "x"},
        ],
    }
    with pytest.raises(RegistryError, match="requires .*unit_id"):
        paired_interval(
            policy, projected, run_id="candidate", champion_run_id="baseline",
            direction="maximize", candidate_metric=.6, champion_metric=.5,
        )


def test_vector_entry_declaring_legacy_scalar_rope_is_refused_at_registration(
    tmp_path: Path,
) -> None:
    registry = Registry(tmp_path)
    entry = metric_entry("ap50")
    entry["adjudication"] = {**entry["adjudication"], "method": "legacy_scalar_rope"}
    with pytest.raises(ContractError, match="ap50.*legacy_scalar_rope|paired_bootstrap"):
        registry.register_campaign_spec(
            spec_mapping(metrics=[entry, metric_entry("idf1")]),
            scoring_corpora=SCORING_CORPORA,
        )


# ---------------------------------------------------------------------------
# Verdicts: Pareto adoption, no regressions
# ---------------------------------------------------------------------------

def test_one_metric_gain_with_the_other_within_rope_is_adopted(tmp_path: Path) -> None:
    registry = vector_registry(tmp_path)
    verdict = adjudicate(registry, shifted({"ap50": .03, "idf1": 0.0}), adopt=True)
    assert verdict == "adopted"
    champion = next(row for row in registry.rows("aliases") if row["alias"] == "champion")
    assert champion["version"] == 2


def test_a_regression_beyond_its_rope_rejects_despite_a_larger_gain_elsewhere(
    tmp_path: Path,
) -> None:
    """THE PLANTED TRADE. ap50 gains a full five points; idf1 regresses two, every paired
    unit agreeing, so its interval sits entirely below zero. The product ships both
    outputs, so the arm does not get to buy one with the other: REJECTED."""
    registry = vector_registry(tmp_path)
    verdict = adjudicate(registry, shifted({"ap50": .05, "idf1": -.02}), adopt=False)
    assert verdict == "rejected"
    payload = _candidate_event(registry)
    evidence = payload["adjudication_evidence"]
    assert evidence["metrics"]["ap50"]["outcome"] == "floor_cleared"
    assert evidence["metrics"]["idf1"]["regressed"] is True
    assert evidence["deciding_metrics"] == ["idf1"]
    assert "idf1" in payload["reason"] and "-0.02" in payload["reason"]
    champion = next(row for row in registry.rows("aliases") if row["alias"] == "champion")
    assert champion["version"] == 1


def test_nothing_cleared_and_nothing_regressed_parks(tmp_path: Path) -> None:
    registry = vector_registry(tmp_path)
    wobble = dict(CHAMPION_UNITS)
    wobble = {"ap50": [round(value + delta, 12) for value, delta in
                       zip(CHAMPION_UNITS["ap50"], [.004, -.004, .004, -.004])],
              "idf1": list(CHAMPION_UNITS["idf1"])}
    assert adjudicate(registry, wobble, adopt=False) == "parked"
    evidence = _candidate_event(registry)["adjudication_evidence"]
    assert evidence["deciding_metrics"] == []
    assert {item["outcome"] for item in evidence["metrics"].values()} == {"within_rope"}


def test_run_missing_a_judged_metric_is_refused_naming_it_never_judged_on_the_subset(
    tmp_path: Path,
) -> None:
    registry = vector_registry(tmp_path)
    units = shifted({"ap50": .03, "idf1": 0.0})
    with pytest.raises(RegistryError, match=r"\['idf1'\].*missing"):
        adjudicate(registry, units, adopt=False,
                   metrics_override={"ap50": aggregates(units)["ap50"]})
    run = next(row for row in registry.rows("runs") if row["run_id"] == "candidate")
    assert run["verdict"] is None


def test_diagnostics_beyond_the_judged_vector_are_ignored(tmp_path: Path) -> None:
    registry = vector_registry(tmp_path)
    units = shifted({"ap50": .03, "idf1": 0.0})
    values = dict(aggregates(units))
    values["ball_localisation_f1"] = .42  # measured, reported, never adjudicated
    assert adjudicate(registry, units, adopt=True, metrics_override=values) == "adopted"
    evidence = _candidate_event(registry)["adjudication_evidence"]
    assert set(evidence["metrics"]) == {"ap50", "idf1"}


def test_vector_evidence_missing_a_judged_metric_on_a_unit_is_refused(
    tmp_path: Path,
) -> None:
    registry = vector_registry(tmp_path)
    units = shifted({"ap50": .03, "idf1": 0.0})
    create_run(registry, "candidate", aggregates(units))
    evidence = vector_evidence(units)
    del evidence["units"][2]["candidate"]["idf1"]
    with pytest.raises(RegistryError, match=r"units\[2\].candidate lacks judged metric 'idf1'"):
        adjudicate_against_champion(
            registry, run_id="candidate", model_id="model", reason="short evidence",
            paired_evidence=evidence,
        )


def test_vector_adjudication_requires_paired_evidence(tmp_path: Path) -> None:
    registry = vector_registry(tmp_path)
    create_run(registry, "candidate", aggregates(shifted({"ap50": .03, "idf1": 0.0})))
    with pytest.raises(RegistryError, match="requires explicit same-unit paired evidence"):
        adjudicate_against_champion(
            registry, run_id="candidate", model_id="model", reason="no evidence",
        )


def test_vector_run_metrics_under_a_scalar_judge_are_refused(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    registry.create_experiment(experiment_id="campaign", spec_digest="a" * 64,
        stages=["representation"], metric="f1", direction="maximize",
        win_condition={}, rope=0.01, baseline_throughput=3.3)
    registry.register_model(model_id="model", family="linear", sport_scope="shared",
                            axis="a01", protocol="Detector", extends=None)
    create_run(registry, "baseline", .68)
    artifact = registry.create_artifact(run_id="baseline", kind="checkpoint",
                                        content=b"base", schema_version="1")
    adopt_run_and_promote(registry, run_id="baseline", model_id="model", reason="bootstrap",
        model_version={"version": 1, "artifact_id": artifact, "checksum": artifact,
        "family_version": "linear@1", "code_sha": SHA, "preprocessing_hash": "prep",
        "calibration": {}, "thresholds": {},
        "compat_result": {"head_sha": SHA, "passed": True, "at": 1}, "status": "active"})
    create_run(registry, "candidate", {"ap50": .70, "idf1": .72})
    with pytest.raises(RegistryError, match="judge is a single scalar metric"):
        adjudicate_against_champion(
            registry, run_id="candidate", model_id="model", reason="vector under scalar",
        )


# ---------------------------------------------------------------------------
# Evidence: a reader can see WHICH output won
# ---------------------------------------------------------------------------

def test_evidence_carries_every_judged_metrics_own_result(tmp_path: Path) -> None:
    registry = vector_registry(tmp_path)
    assert adjudicate(registry, shifted({"ap50": .03, "idf1": 0.0}), adopt=True) == "adopted"
    payload = _candidate_event(registry)
    evidence = payload["adjudication_evidence"]
    assert evidence["method"] == "vector_pareto"
    assert evidence["judged_metrics"] == ["ap50", "idf1"]
    ap50 = evidence["metrics"]["ap50"]
    assert ap50["gain"] == pytest.approx(.03)
    assert ap50["adoption_floor"] == pytest.approx(.005)
    assert ap50["floor_cleared"] is True and ap50["regressed"] is False
    assert ap50["outcome"] == "floor_cleared"
    assert ap50["interval"][0] > 0
    assert ap50["method"] == "paired_bootstrap_percentile" and len(ap50["units"]) == 4
    idf1 = evidence["metrics"]["idf1"]
    assert idf1["outcome"] == "within_rope"
    assert idf1["gain"] == pytest.approx(0.0)
    assert evidence["deciding_metrics"] == ["ap50"]
    assert "ap50" in payload["reason"] and "no judged metric regressed" in payload["reason"]


def test_vector_adoption_retry_is_idempotent_and_evidence_drift_is_refused(
    tmp_path: Path,
) -> None:
    registry = vector_registry(tmp_path)
    units = shifted({"ap50": .03, "idf1": 0.0})
    create_run(registry, "candidate", aggregates(units))
    inputs = promotion(registry, "candidate")
    evidence = vector_evidence(units)
    assert adjudicate_against_champion(
        registry, run_id="candidate", model_id="model", reason="vector comparison",
        promotion=inputs, paired_evidence=evidence,
    ) == "adopted"
    count = len(registry.list_events())
    assert adjudicate_against_champion(
        registry, run_id="candidate", model_id="model", reason="vector comparison",
        promotion=inputs, paired_evidence=evidence,
    ) == "adopted"
    assert len(registry.list_events()) == count
    drifted = json.loads(json.dumps(evidence))
    drifted["units"][0]["candidate"]["ap50"] = .99
    with pytest.raises(RegistryError, match="drifted from its paired evidence"):
        adjudicate_against_champion(
            registry, run_id="candidate", model_id="model", reason="vector comparison",
            promotion=inputs, paired_evidence=drifted,
        )
