"""The durable per-stratum breakdown every paired aggregation reports.

A frozen interval over the whole paired sample says whether a run won; it does not say WHERE
it won. These tests pin the one breakdown shape each branch reports -- unit count, candidate
mean, champion mean, direction-signed delta, per declared stratum or domain -- and pin that it
is reporting only: a stratum that regressed cannot move the verdict the interval already fixed.
"""

from __future__ import annotations

import pytest

from knowledge.ml_registry.services.paired_adjudication import (
    STRATUM_BREAKDOWN,
    paired_interval,
    project_vector_evidence,
    stratum_breakdown,
)


def _policy(aggregation: str, **overrides: object) -> dict[str, object]:
    return {
        "method": "paired_bootstrap_percentile",
        "resamples": 200,
        "confidence_level": 0.95,
        "seed": 20260828,
        "aggregation": aggregation,
        **overrides,
    }


def _evidence(units: list[dict[str, object]]) -> dict[str, object]:
    return {
        "candidate_run_id": "candidate",
        "champion_run_id": "champion",
        "baseline_run_id": "champion",
        "resamples": 200,
        "confidence_level": 0.95,
        "seed": 20260828,
        "units": units,
    }


#: Two strata pulling in OPPOSITE directions: `wide` gains, `close` regresses. The macro over
#: strata still gains, so this sample is exactly where a breakdown earns its keep.
_MACRO_UNITS = [
    {"unit_id": "w1", "stratum": "wide", "candidate": 0.90, "champion": 0.60},
    {"unit_id": "w2", "stratum": "wide", "candidate": 0.70, "champion": 0.40},
    {"unit_id": "c1", "stratum": "close", "candidate": 0.30, "champion": 0.40},
    {"unit_id": "c2", "stratum": "close", "candidate": 0.10, "champion": 0.20},
]
# wide: candidate 0.80 vs champion 0.50. close: candidate 0.20 vs champion 0.30.
_MACRO_CANDIDATE = 0.5
_MACRO_CHAMPION = 0.4


def test_macro_strata_reports_every_declared_stratum() -> None:
    interval = paired_interval(
        _policy("macro_strata"), _evidence(list(_MACRO_UNITS)), run_id="candidate",
        champion_run_id="champion", direction="maximize",
        candidate_metric=_MACRO_CANDIDATE, champion_metric=_MACRO_CHAMPION,
    )

    assert interval.evidence[STRATUM_BREAKDOWN] == [
        {"stratum": "close", "unit_count": 2, "candidate_mean": pytest.approx(0.20),
         "champion_mean": pytest.approx(0.30), "delta": pytest.approx(-0.10)},
        {"stratum": "wide", "unit_count": 2, "candidate_mean": pytest.approx(0.80),
         "champion_mean": pytest.approx(0.50), "delta": pytest.approx(0.30)},
    ]
    # Every declared stratum, and only those -- the breakdown names exactly what `strata` does.
    assert [row["stratum"] for row in interval.evidence[STRATUM_BREAKDOWN]] == \
        interval.evidence["strata"]
    assert sum(row["unit_count"] for row in interval.evidence[STRATUM_BREAKDOWN]) == \
        interval.evidence["unit_count"]


def test_a_regressing_stratum_does_not_move_the_frozen_verdict() -> None:
    """`close` regressed by 0.10; the verdict stays the interval over the whole sample."""
    interval = paired_interval(
        _policy("macro_strata"), _evidence(list(_MACRO_UNITS)), run_id="candidate",
        champion_run_id="champion", direction="maximize",
        candidate_metric=_MACRO_CANDIDATE, champion_metric=_MACRO_CHAMPION,
    )
    rows = {row["stratum"]: row for row in interval.evidence[STRATUM_BREAKDOWN]}

    assert rows["close"]["delta"] < 0.0
    assert interval.point_estimate == pytest.approx(_MACRO_CANDIDATE - _MACRO_CHAMPION)
    assert interval.evidence["point_estimate"] == pytest.approx(interval.point_estimate)
    assert interval.evidence["interval"] == [interval.lower, interval.upper]
    assert interval.lower <= interval.point_estimate <= interval.upper


def test_mean_reports_its_single_undeclared_stratum_in_the_same_shape() -> None:
    interval = paired_interval(
        _policy("mean"),
        _evidence([
            {"unit_id": "a", "candidate": 0.80, "champion": 0.60},
            {"unit_id": "b", "candidate": 0.60, "champion": 0.40},
        ]),
        run_id="candidate", champion_run_id="champion", direction="maximize",
        candidate_metric=0.7, champion_metric=0.5,
    )

    assert interval.evidence[STRATUM_BREAKDOWN] == [
        {"stratum": "all", "unit_count": 2, "candidate_mean": pytest.approx(0.70),
         "champion_mean": pytest.approx(0.50), "delta": pytest.approx(0.20)},
    ]


def test_the_breakdown_delta_is_signed_by_direction_like_the_point_estimate() -> None:
    """Under `minimize`, a stratum whose scores FELL improved -- and reads positive."""
    interval = paired_interval(
        _policy("macro_strata"), _evidence(list(_MACRO_UNITS)), run_id="candidate",
        champion_run_id="champion", direction="minimize",
        candidate_metric=_MACRO_CANDIDATE, champion_metric=_MACRO_CHAMPION,
    )
    rows = {row["stratum"]: row for row in interval.evidence[STRATUM_BREAKDOWN]}

    assert rows["close"]["delta"] == pytest.approx(0.10)
    assert rows["wide"]["delta"] == pytest.approx(-0.30)
    assert interval.point_estimate == pytest.approx(_MACRO_CHAMPION - _MACRO_CANDIDATE)


def test_nested_macro_reports_one_row_per_truth_kind_corpus_domain() -> None:
    units = [
        {"unit_id": "d1", "truth_kind": "direct", "corpus": "one", "candidate": 0.90,
         "champion": 0.80},
        {"unit_id": "d2", "truth_kind": "direct", "corpus": "one", "candidate": 0.70,
         "champion": 0.60},
        {"unit_id": "v1", "truth_kind": "derived", "corpus": "two", "candidate": 0.60,
         "champion": 0.50},
        {"unit_id": "v2", "truth_kind": "derived", "corpus": "two", "candidate": 0.40,
         "champion": 0.30},
    ]
    candidate = (0.80 + 0.50) / 2.0
    champion = (0.70 + 0.40) / 2.0
    interval = paired_interval(
        _policy("macro_truth_kind_corpus_group"), _evidence(units), run_id="candidate",
        champion_run_id="champion", direction="maximize",
        candidate_metric=candidate, champion_metric=champion,
    )

    assert interval.evidence[STRATUM_BREAKDOWN] == [
        {"stratum": "derived:two", "unit_count": 2, "candidate_mean": pytest.approx(0.50),
         "champion_mean": pytest.approx(0.40), "delta": pytest.approx(0.10)},
        {"stratum": "direct:one", "unit_count": 2, "candidate_mean": pytest.approx(0.80),
         "champion_mean": pytest.approx(0.70), "delta": pytest.approx(0.10)},
    ]
    # The breakdown names the SAME domains, spelled the same way, as the durable strata list.
    assert [row["stratum"] for row in interval.evidence[STRATUM_BREAKDOWN]] == \
        interval.evidence["strata"]


def test_pooled_counts_reports_the_pooled_stratum_score_not_a_per_unit_mean() -> None:
    """An F1 is a ratio of sums, so the stratum's row carries its POOLED score."""
    units = [
        {"unit_id": "g1", "stratum": "release-a",
         "candidate": {"small": {"2": [1, 0, 1]}}, "champion": {"small": {"2": [1, 0, 1]}}},
        {"unit_id": "g2", "stratum": "release-a",
         "candidate": {"small": {"2": [0, 2, 0]}}, "champion": {"small": {"2": [0, 1, 2]}}},
    ]
    interval = paired_interval(
        _policy("pooled_counts_over_resampled_groups"), _evidence(units), run_id="candidate",
        champion_run_id="champion", direction="maximize",
        candidate_metric=0.4, champion_metric=1.0 / 3.0,
    )

    assert interval.evidence[STRATUM_BREAKDOWN] == [
        {"stratum": "release-a", "unit_count": 2, "candidate_mean": pytest.approx(0.4),
         "champion_mean": pytest.approx(1.0 / 3.0),
         "delta": pytest.approx(0.4 - 1.0 / 3.0)},
    ]
    # 1/3 is the mean of the two units' own candidate F1s -- the number this row must NOT be.
    assert interval.evidence[STRATUM_BREAKDOWN][0]["candidate_mean"] != pytest.approx(1.0 / 3.0)


def test_a_vector_metric_projection_carries_the_same_breakdown() -> None:
    """A vector run projects one metric out of a shared unit list; reporting follows it."""
    evidence = _evidence([
        {"unit_id": "w1", "stratum": "wide",
         "candidate": {"ap50": 0.90}, "champion": {"ap50": 0.60}},
        {"unit_id": "c1", "stratum": "close",
         "candidate": {"ap50": 0.30}, "champion": {"ap50": 0.40}},
        {"unit_id": "c2", "stratum": "close",
         "candidate": {"ap50": 0.10}, "champion": {"ap50": 0.20}},
        {"unit_id": "s1", "stratum": "wide", "candidate": {"idf1": 0.5}, "champion": {"idf1": 0.5}},
    ])
    projected = project_vector_evidence(evidence, "ap50")
    interval = paired_interval(
        _policy("mean"), projected, run_id="candidate", champion_run_id="champion",
        direction="maximize", candidate_metric=(0.90 + 0.30 + 0.10) / 3.0,
        champion_metric=(0.60 + 0.40 + 0.20) / 3.0,
    )

    assert interval.evidence[STRATUM_BREAKDOWN] == [
        {"stratum": "all", "unit_count": 3,
         "candidate_mean": pytest.approx((0.90 + 0.30 + 0.10) / 3.0),
         "champion_mean": pytest.approx((0.60 + 0.40 + 0.20) / 3.0),
         "delta": pytest.approx(0.1 / 3.0)},
    ]


def test_stratum_breakdown_is_reusable_on_its_own_rows() -> None:
    """The helper is the reusable seam: any branch hands it (stratum, candidate, champion)."""
    rows = [("b", 0.4, 0.2), ("a", 0.1, 0.5), ("a", 0.3, 0.5)]

    assert stratum_breakdown(rows, sign=1.0) == [
        {"stratum": "a", "unit_count": 2, "candidate_mean": pytest.approx(0.20),
         "champion_mean": pytest.approx(0.50), "delta": pytest.approx(-0.30)},
        {"stratum": "b", "unit_count": 1, "candidate_mean": pytest.approx(0.40),
         "champion_mean": pytest.approx(0.20), "delta": pytest.approx(0.20)},
    ]
