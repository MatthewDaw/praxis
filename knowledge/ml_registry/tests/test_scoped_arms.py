from __future__ import annotations

import pytest

from knowledge.ml_registry.services.scoped_arms import (
    scope_from_params,
    scoped_interval,
    validate_constituents,
    validate_scope,
)


def test_scope_is_exact_and_must_be_declared() -> None:
    scope = scope_from_params({"scope": {"group": "sport", "value": "soccer"}})
    assert scope == {"group": "sport", "value": "soccer"}
    validate_scope(scope, {"metric": {"groups": {"sport": {"field": "sport"}}}})
    with pytest.raises(ValueError, match="undeclared group"):
        validate_scope(scope, {"metric": {"groups": {"corpus": {"field": "corpus"}}}})
    with pytest.raises(ValueError, match="exactly"):
        scope_from_params({"scope": {"group": "sport"}})


def test_fold_constituents_must_be_measured_and_non_regressed() -> None:
    class Event:
        event_type = "run_adjudicated"
        payload = {"run_id": "parked", "adjudication_evidence": {"metrics": {"m": {"regressed": True}}}}

    class Registry:
        def rows(self, table):
            assert table == "runs"
            return [
                {"run_id": "adopted", "experiment_id": "e", "status": "succeeded", "verdict": "adopted"},
                {"run_id": "parked", "experiment_id": "e", "status": "succeeded", "verdict": "parked"},
            ]
        def list_events(self):
            return [Event()]

    registry = Registry()
    validate_constituents(registry, experiment_id="e", run_id="fold", params={"constituents": ["adopted"]})
    with pytest.raises(ValueError, match="regression"):
        validate_constituents(registry, experiment_id="e", run_id="fold", params={"constituents": ["parked"]})
    with pytest.raises(ValueError, match="not been measured"):
        validate_constituents(registry, experiment_id="e", run_id="fold", params={"constituents": ["missing"]})


def test_scoped_interval_bootstraps_only_the_target_group() -> None:
    policy = {"method": "paired_bootstrap_percentile", "resamples": 100,
              "confidence_level": .9, "seed": 3, "aggregation": "mean"}
    evidence = {"candidate_run_id": "candidate", "champion_run_id": "champion",
                "baseline_run_id": "champion", "resamples": 100,
                "confidence_level": .9, "seed": 3, "units": [
        {"unit_id": "s1", "stratum": "soccer", "candidate": .9, "champion": .7},
        {"unit_id": "s2", "stratum": "soccer", "candidate": .8, "champion": .6},
        {"unit_id": "t1", "stratum": "tennis", "candidate": .3, "champion": .8},
        {"unit_id": "t2", "stratum": "tennis", "candidate": .4, "champion": .9},
    ]}
    interval = scoped_interval(evidence, scope={"group": "sport", "value": "soccer"}, policy=policy,
                               run_id="candidate", champion_run_id="champion", direction="maximize")
    assert interval.lower > 0
    assert interval.evidence["strata"] == ["soccer"]
