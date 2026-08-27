"""The vector's DIMENSION is the campaign's to change: a judged metric may be ADDED, or
REMOVED, after registration.

The right objectives are rarely knowable in Phase B, and a campaign stuck optimising the
wrong set is worse off than one that re-freezes. Four things make it honest and all four
are proved here: the whole new vector revalidates exactly as registration validates it, a
written reason is required, a removed metric is DEMOTED to a diagnostic rather than
deleted, and a metric that is currently deciding cannot be removed. A vector change is
also a re-baseline -- adjudication refuses to pair across it rather than warning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowledge.ml_registry import Registry
from knowledge.ml_registry.services.paired_adjudication import (
    campaign_diagnostic_metrics,
    campaign_judged_metrics,
    campaign_metric,
)
from knowledge.ml_registry.services.registry_adjudication import adjudicate_against_champion
from knowledge.ml_registry.services.registry_aliases import adopt_run_and_promote
from knowledge.ml_registry.storage import RegistryError

from .test_vector_adjudication import (
    CHAMPION_METRICS,
    CHAMPION_UNITS,
    SHA,
    aggregates,
    create_run,
    metric_entry,
    promotion,
    shifted,
    spec_mapping,
    vector_registry,
)


SCORING_CORPORA = {"fixture": [
    {"unit_id": "one", "ap50": .59, "idf1": .69, "recall": .49},
    {"unit_id": "two", "ap50": .61, "idf1": .71, "recall": .51},
]}

CHAMPION_RECALL = [.49, .50, .51, .50]
CHAMPION_UNITS_PLUS = {**CHAMPION_UNITS, "recall": CHAMPION_RECALL}

REASON_ADD = "the product consumes recall and we were not measuring it"
REASON_REMOVE = "idf1 and ap50 moved together on every arm; idf1 is a proxy for ap50"


def amend(registry: Registry, names: list[str], reason: str) -> bool:
    return registry.amend_judged_vector(
        "campaign",
        metrics=[metric_entry(name) for name in names],
        reason=reason,
        scoring_corpora=SCORING_CORPORA,
    )


def rebaseline(registry: Registry, run_id: str, values: dict[str, float], version: int) -> None:
    """Re-measure the champion under the amended vector and promote it as the baseline.

    A vector amendment does not move a number by itself, so the re-baseline is a promotion,
    never an adoption: nothing is recorded as a win.
    """
    create_run(registry, run_id, values)
    artifact = registry.create_artifact(run_id=run_id, kind="checkpoint",
                                        content=f"rebase:{run_id}".encode(), schema_version="1")
    adopt_run_and_promote(
        registry, run_id=run_id, model_id="model", reason="re-baseline under amended vector",
        model_version={"version": version, "artifact_id": artifact, "checksum": artifact,
                       "family_version": "linear@1", "code_sha": SHA, "preprocessing_hash": "prep",
                       "calibration": {}, "thresholds": {},
                       "compat_result": {"head_sha": SHA, "passed": True, "at": 2},
                       "status": "active"})


def evidence_for(candidate_units: dict[str, list[float]], champion_run_id: str,
                 champion_units: dict[str, list[float]] | None = None) -> dict[str, object]:
    """Same-unit paired evidence over whatever metric names a run reports -- judged and
    demoted alike, because a diagnostic goes on being measured on every unit."""
    champion_units = champion_units or CHAMPION_UNITS_PLUS
    names = sorted(candidate_units)
    return {
        "candidate_run_id": "candidate",
        "champion_run_id": champion_run_id,
        "resamples": 500,
        "confidence_level": .95,
        "seed": 17,
        "units": [
            {"unit_id": f"unit-{index}",
             "candidate": {name: candidate_units[name][index] for name in names},
             "champion": {name: champion_units[name][index] for name in names}}
            for index in range(4)
        ],
    }


def judged_names(registry: Registry) -> list[str] | None:
    entries = campaign_judged_metrics(registry, "campaign")
    return None if entries is None else [str(entry["name"]) for entry in entries]


def amendment(registry: Registry) -> dict:
    return next(event.payload for event in reversed(registry.list_events())
                if event.event_type == "campaign_vector_amended")


# ---------------------------------------------------------------------------
# Adding a metric
# ---------------------------------------------------------------------------

def test_adding_a_metric_makes_adjudication_require_it(tmp_path: Path) -> None:
    registry = vector_registry(tmp_path)
    assert amend(registry, ["ap50", "idf1", "recall"], REASON_ADD)
    assert judged_names(registry) == ["ap50", "idf1", "recall"]

    # The champion is re-measured under the amended vector and promoted as the baseline.
    rebaseline(registry, "rebaseline", {**CHAMPION_METRICS, "recall": .50}, version=2)

    # A run that reports only the OLD vector is now INVALID: the new metric is judged.
    units = {**shifted({"ap50": .03, "idf1": 0.0}), "recall": CHAMPION_RECALL}
    create_run(registry, "candidate", aggregates({name: units[name]
                                                  for name in ("ap50", "idf1")}))
    with pytest.raises(RegistryError, match=r"\['recall'\].*missing"):
        adjudicate_against_champion(
            registry, run_id="candidate", model_id="model", reason="missing the new metric",
            paired_evidence=evidence_for(units, "rebaseline"),
        )


def test_adding_a_metric_recomputes_the_rope_for_the_new_one(tmp_path: Path) -> None:
    registry = vector_registry(tmp_path)
    amend(registry, ["ap50", "idf1", "recall"], REASON_ADD)
    rope = amendment(registry)["spec"]["rope"]
    assert rope["method"] == "vector"
    assert set(rope["metrics"]) == {"ap50", "idf1", "recall"}
    assert rope["metrics"]["recall"]["method"] == "split_unit_bootstrap"
    assert rope["metrics"]["recall"]["metric"] == "recall"


def test_an_amended_vector_revalidates_exactly_as_registration_does(tmp_path: Path) -> None:
    registry = vector_registry(tmp_path)
    entry = metric_entry("recall")
    entry["adjudication"] = {**entry["adjudication"], "method": "legacy_scalar_rope"}
    with pytest.raises(RegistryError, match="recall.*legacy_scalar_rope|paired_bootstrap"):
        registry.amend_judged_vector(
            "campaign", metrics=[metric_entry("ap50"), metric_entry("idf1"), entry],
            reason=REASON_ADD, scoring_corpora=SCORING_CORPORA,
        )
    assert judged_names(registry) == ["ap50", "idf1"]
    assert not any(event.event_type == "campaign_vector_amended"
                   for event in registry.list_events())


# ---------------------------------------------------------------------------
# Removing a metric: demoted, never deleted
# ---------------------------------------------------------------------------

def test_a_removed_metric_becomes_a_diagnostic_and_stops_adjudicating(tmp_path: Path) -> None:
    registry = vector_registry(tmp_path)
    assert amend(registry, ["ap50"], REASON_REMOVE)
    # Demoted, never deleted: still declared, still measured, still in evidence.
    diagnostics = campaign_diagnostic_metrics(registry, "campaign")
    assert [str(item["name"]) for item in diagnostics] == ["idf1"]
    assert diagnostics[0]["direction"] == "maximize"
    # A single-entry vector IS the scalar judge, so the campaign now judges one metric.
    assert judged_names(registry) is None
    assert campaign_metric(registry, "campaign")["name"] == "ap50"

    rebaseline(registry, "rebaseline", CHAMPION_METRICS, version=2)
    units = shifted({"ap50": .03, "idf1": -.04})
    create_run(registry, "candidate", aggregates(units))
    # The run STILL REPORTS idf1, on every unit -- it fell four points, and it decides
    # nothing: ap50's gain alone is the verdict.
    assert adjudicate_against_champion(
        registry, run_id="candidate", model_id="model", reason="ap50 only",
        promotion=promotion(registry, "candidate", version=3),
        paired_evidence=evidence_for(units, "rebaseline", CHAMPION_UNITS),
    ) == "adopted"
    reported = next(row for row in registry.rows("runs") if row["run_id"] == "candidate")
    assert "idf1" in reported["metrics"]


def test_a_single_metric_campaign_can_be_amended_into_a_vector(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    registry.create_experiment(experiment_id="campaign", spec_digest="a" * 64,
        stages=["representation"], metric="ap50", direction="maximize",
        win_condition={"metric_at_least": 0.9}, rope=0.01, baseline_throughput=3.3)
    registry.register_campaign_spec(spec_mapping(metric=metric_entry("ap50")),
                                    scoring_corpora=SCORING_CORPORA)
    assert judged_names(registry) is None
    assert amend(registry, ["ap50", "idf1"], "the product consumes identity persistence too")
    assert judged_names(registry) == ["ap50", "idf1"]
    assert campaign_diagnostic_metrics(registry, "campaign") == ()


# ---------------------------------------------------------------------------
# The abuse guard
# ---------------------------------------------------------------------------

def test_removing_the_metric_that_caused_the_last_rejection_is_refused_by_name(
    tmp_path: Path,
) -> None:
    """THE PLANTED ABUSE. The arm buys five points of ap50 with two points of idf1 and is
    REJECTED on idf1. Removing idf1 next is removing the referee that just ruled against
    you, and it is refused naming the metric, the run, and the regression."""
    registry = vector_registry(tmp_path)
    units = shifted({"ap50": .05, "idf1": -.02})
    create_run(registry, "candidate", aggregates(units))
    assert adjudicate_against_champion(
        registry, run_id="candidate", model_id="model", reason="a trade",
        paired_evidence=evidence_for(units, "baseline", CHAMPION_UNITS),
    ) == "rejected"

    with pytest.raises(RegistryError, match="cannot remove 'idf1'.*currently deciding"):
        amend(registry, ["ap50"], REASON_REMOVE)
    with pytest.raises(RegistryError) as excinfo:
        amend(registry, ["ap50"], REASON_REMOVE)
    text = str(excinfo.value)
    assert "'candidate'" in text and "REJECTED" in text and "-0.02" in text
    # Nothing moved.
    assert judged_names(registry) == ["ap50", "idf1"]
    assert campaign_diagnostic_metrics(registry, "campaign") == ()

    # The metric that did NOT decide can still be removed, and the deciding one may be
    # removed once an arm is judged without it deciding.
    assert amend(registry, ["idf1"], "ap50 turned out to be a proxy for idf1")


def test_an_empty_reason_is_refused_by_name(tmp_path: Path) -> None:
    registry = vector_registry(tmp_path)
    with pytest.raises(RegistryError, match="requires a reason"):
        amend(registry, ["ap50", "idf1", "recall"], "   ")
    assert judged_names(registry) == ["ap50", "idf1"]


def test_an_unchanged_vector_is_not_an_amendment(tmp_path: Path) -> None:
    registry = vector_registry(tmp_path)
    with pytest.raises(RegistryError, match="requires an added or removed judged metric"):
        amend(registry, ["ap50", "idf1"], "nothing changed")


def test_amending_an_unregistered_campaign_is_refused(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    with pytest.raises(RegistryError, match="unknown campaign spec 'campaign'"):
        amend(registry, ["ap50"], REASON_REMOVE)


# ---------------------------------------------------------------------------
# The re-baseline
# ---------------------------------------------------------------------------

def test_pairing_across_an_un_re_baselined_amendment_is_refused(tmp_path: Path) -> None:
    registry = vector_registry(tmp_path)
    amend(registry, ["ap50", "idf1", "recall"], REASON_ADD)
    units = {**shifted({"ap50": .03, "idf1": 0.0}), "recall": CHAMPION_RECALL}
    create_run(registry, "candidate", aggregates(units))
    with pytest.raises(RegistryError, match="not comparable") as excinfo:
        adjudicate_against_champion(
            registry, run_id="candidate", model_id="model", reason="stale champion",
            paired_evidence=evidence_for(units, "baseline"),
        )
    text = str(excinfo.value)
    assert "'baseline'" in text and "'candidate'" in text
    assert "['ap50', 'idf1']" in text and "['ap50', 'idf1', 'recall']" in text
    assert "re-measure the champion" in text.lower()
    run = next(row for row in registry.rows("runs") if row["run_id"] == "candidate")
    assert run["verdict"] is None


def test_a_fresh_baseline_promoted_under_the_new_vector_lets_arms_resume(
    tmp_path: Path,
) -> None:
    registry = vector_registry(tmp_path)
    amend(registry, ["ap50", "idf1", "recall"], REASON_ADD)
    rebaseline(registry, "rebaseline", {**CHAMPION_METRICS, "recall": .50}, version=2)
    units = {**shifted({"ap50": .03, "idf1": 0.0}), "recall": CHAMPION_RECALL}
    create_run(registry, "candidate", aggregates(units))
    assert adjudicate_against_champion(
        registry, run_id="candidate", model_id="model", reason="amended vector",
        promotion=promotion(registry, "candidate", version=3),
        paired_evidence=evidence_for(units, "rebaseline"),
    ) == "adopted"


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

def test_the_amendment_is_recorded_in_the_event_log(tmp_path: Path) -> None:
    registry = vector_registry(tmp_path)
    amend(registry, ["ap50", "recall"], "the product consumes recall; idf1 proxies ap50")
    event = next(event for event in reversed(registry.list_events())
                 if event.event_type == "campaign_vector_amended")
    assert event.payload["campaign_id"] == "campaign"
    assert event.payload["reason"].startswith("the product consumes recall")
    assert event.payload["added"] == ["recall"]
    assert event.payload["removed"] == ["idf1"]
    assert event.payload["old"]["judged_metrics"] == ["ap50", "idf1"]
    assert event.payload["new"]["judged_metrics"] == ["ap50", "recall"]
    assert event.payload["new"]["diagnostic_metrics"] == ["idf1"]
    assert event.at > 0
