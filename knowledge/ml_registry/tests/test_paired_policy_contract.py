"""Regression coverage for the frozen no-floor paired-interval judge."""

from __future__ import annotations

import pytest

from knowledge.ml_registry.services.paired_adjudication import evidence_digest, paired_interval
from knowledge.ml_registry.storage import RegistryError


_FROZEN_LAW = (
    "95% paired interval entirely positive ADOPT, crosses zero PARK, "
    "entirely negative REJECT"
)


def _policy(**overrides: object) -> dict[str, object]:
    return {
        "method": "paired_bootstrap_percentile",
        "resamples": 100,
        "confidence_level": 0.95,
        "seed": 17,
        "aggregation": "stitch_decision",
        "effect_floor": 0.0,
        "law": _FROZEN_LAW,
        **overrides,
    }


def _evidence() -> dict[str, object]:
    return {
        "candidate_run_id": "candidate",
        "champion_run_id": "champion",
        "resamples": 100,
        "confidence_level": 0.95,
        "seed": 17,
        "units": [
            {"unit_id": "a", "candidate": 0.8, "champion": 0.6},
            {"unit_id": "b", "candidate": 0.6, "champion": 0.4},
        ],
    }


def test_frozen_no_floor_stitch_policy_is_accepted_as_mean() -> None:
    interval = paired_interval(
        _policy(), _evidence(), run_id="candidate", champion_run_id="champion",
        direction="maximize", candidate_metric=0.7, champion_metric=0.5,
    )

    assert interval.evidence["aggregation"] == "mean"
    assert interval.point_estimate == pytest.approx(0.2)


@pytest.mark.parametrize(
    "policy, message",
    [
        (_policy(effect_floor=0.001), "only effect_floor=0.0"),
        (_policy(law="paired improvement must exceed 0.01"), "zero-threshold CI rule"),
    ],
)
def test_frozen_no_floor_policy_refuses_any_relaxed_or_changed_law(
    policy: dict[str, object], message: str,
) -> None:
    with pytest.raises(RegistryError, match=message):
        paired_interval(
            policy, _evidence(), run_id="candidate", champion_run_id="champion",
            direction="maximize", candidate_metric=0.7, champion_metric=0.5,
        )


# --------------------------------------------------------------------------------------------
# pooled_counts_over_resampled_groups -- the branch for a judge whose scalar is an F1 over POOLED
# counts. An F1 is a ratio of sums, so no per-unit scalar mean can reproduce it; these tests pin
# that the branch computes the pooled aggregate, refuses when it disagrees with the registered Run
# metric, and leaves the scalar aggregations untouched.
# --------------------------------------------------------------------------------------------


def _pooled_policy(**overrides: object) -> dict[str, object]:
    return {
        "method": "paired_bootstrap_percentile",
        "resamples": 64,
        "confidence_level": 0.95,
        "seed": 20260827,
        "aggregation": "pooled_counts_over_resampled_groups",
        **overrides,
    }


def _pooled_units() -> list[dict[str, object]]:
    # Two units, one stratum. The candidate pools to 1 TP / 2 FP / 1 FN in the one scale stratum,
    # an F1 of 0.4 -- while the mean of its two units' own F1s is 0.333..., a DIFFERENT number.
    # That gap is the entire reason this aggregation exists: an F1 is a ratio of sums.
    return [
        {"unit_id": "g1", "stratum": "release-a",
         "candidate": {"small": {"2": [1, 0, 1]}}, "champion": {"small": {"2": [1, 0, 1]}}},
        {"unit_id": "g2", "stratum": "release-a",
         "candidate": {"small": {"2": [0, 2, 0]}}, "champion": {"small": {"2": [0, 1, 2]}}},
    ]


def _pooled_evidence(**overrides: object) -> dict[str, object]:
    return {
        "candidate_run_id": "candidate",
        "champion_run_id": "champion",
        "resamples": 64,
        "confidence_level": 0.95,
        "seed": 20260827,
        "units": _pooled_units(),
        **overrides,
    }


def test_pooled_counts_adjudicates_on_the_pooled_f1_not_a_per_unit_mean() -> None:
    interval = paired_interval(
        _pooled_policy(), _pooled_evidence(), run_id="candidate", champion_run_id="champion",
        direction="maximize", candidate_metric=0.4, champion_metric=1.0 / 3.0,
    )

    assert interval.evidence["aggregation"] == "pooled_counts_over_resampled_groups"
    assert interval.evidence["unit_count"] == 2
    assert interval.evidence["strata"] == ["release-a"]
    assert interval.point_estimate == pytest.approx(0.4 - 1.0 / 3.0)
    assert interval.lower <= interval.point_estimate <= interval.upper
    # registry_adjudication reads .evidence/.lower/.upper off this value and stamps the digest
    # into the durable ledger row, so the branch owes the same object the scalar ones return.
    assert interval.evidence["input_sha256"] == evidence_digest(_pooled_evidence())
    assert interval.evidence["units"] == _pooled_units()


def test_pooled_counts_refuses_when_the_aggregate_is_not_the_registered_metric() -> None:
    # 1/3 is the MEAN of the two units' candidate F1s -- what a `mean` aggregation would have
    # adjudicated. The pooled aggregate is 0.4, so supplying 1/3 as the registered Run metric must
    # refuse rather than quietly judge a different scalar than the one the Run reported.
    with pytest.raises(RegistryError, match="candidate aggregate differs"):
        paired_interval(
            _pooled_policy(), _pooled_evidence(), run_id="candidate",
            champion_run_id="champion", direction="maximize",
            candidate_metric=1.0 / 3.0, champion_metric=1.0 / 3.0,
        )


@pytest.mark.parametrize(
    "unit, message",
    [
        ({"unit_id": "g2", "stratum": "release-a", "candidate": {"small": {"2": [1, 0]}},
          "champion": {"small": {"2": [0, 1, 2]}}}, r"\[tp, fp, fn\]"),
        ({"unit_id": "g2", "stratum": "release-a", "candidate": {"small": {"2": [1, 0, -1]}},
          "champion": {"small": {"2": [0, 1, 2]}}}, "non-negative integers"),
        ({"unit_id": "g1", "stratum": "release-a", "candidate": {"small": {"2": [1, 0, 1]}},
          "champion": {"small": {"2": [0, 1, 2]}}}, "repeats unit_id"),
    ],
)
def test_pooled_counts_refuses_malformed_units(unit: dict[str, object], message: str) -> None:
    units = [_pooled_units()[0], unit]
    with pytest.raises(RegistryError, match=message):
        paired_interval(
            _pooled_policy(), _pooled_evidence(units=units), run_id="candidate",
            champion_run_id="champion", direction="maximize",
            candidate_metric=0.4, champion_metric=1.0 / 3.0,
        )


def test_unknown_aggregation_is_still_refused() -> None:
    with pytest.raises(RegistryError, match="aggregation must be"):
        paired_interval(
            _pooled_policy(aggregation="pooled"), _pooled_evidence(), run_id="candidate",
            champion_run_id="champion", direction="maximize",
            candidate_metric=0.4, champion_metric=1.0 / 3.0,
        )
