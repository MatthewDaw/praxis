"""The three-level ``macro_truth_kind_corpus_group`` paired aggregation.

Its reason to exist is the first test: a campaign whose frozen scalar means units within a
corpus, corpora within a truth kind, and then truth kinds, computes a DIFFERENT number from the
two-level ``macro_strata`` whenever its cells hold unequal unit counts. Declaring the two-level
aggregation would adjudicate a scalar other than the one measured and registered, which is the
drift ``paired_interval`` exists to refuse.
"""

from __future__ import annotations

import pytest

from knowledge.ml_registry.services.paired_adjudication import paired_interval
from knowledge.ml_registry.storage import RegistryError


#: Two truth kinds; the derived kind holds two corpora of unequal size, so the nested macro and
#: the flat macro over the same four cells disagree.
_UNITS = [
    {"unit_id": "d:one:g1", "truth_kind": "direct", "corpus": "one", "candidate": 0.90,
     "champion": 0.80},
    {"unit_id": "d:one:g2", "truth_kind": "direct", "corpus": "one", "candidate": 0.70,
     "champion": 0.60},
    {"unit_id": "v:two:g1", "truth_kind": "derived", "corpus": "two", "candidate": 0.60,
     "champion": 0.50},
    {"unit_id": "v:two:g2", "truth_kind": "derived", "corpus": "two", "candidate": 0.40,
     "champion": 0.30},
    {"unit_id": "v:three:g1", "truth_kind": "derived", "corpus": "three", "candidate": 0.20,
     "champion": 0.10},
    {"unit_id": "v:three:g2", "truth_kind": "derived", "corpus": "three", "candidate": 0.10,
     "champion": 0.10},
]
# direct: mean(0.90, 0.70) = 0.80. derived: mean(mean(0.60, 0.40), mean(0.20, 0.10)) = 0.325.
_CANDIDATE = (0.80 + 0.325) / 2.0
# direct: mean(0.80, 0.60) = 0.70. derived: mean(mean(0.50, 0.30), mean(0.10, 0.10)) = 0.25.
_CHAMPION = (0.70 + 0.25) / 2.0


def _policy(**overrides: object) -> dict[str, object]:
    return {
        "method": "paired_bootstrap_percentile",
        "resamples": 200,
        "confidence_level": 0.95,
        "seed": 20260826,
        "aggregation": "macro_truth_kind_corpus_group",
        **overrides,
    }


def _evidence(units: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "candidate_run_id": "candidate",
        "champion_run_id": "champion",
        "resamples": 200,
        "confidence_level": 0.95,
        "seed": 20260826,
        "units": list(_UNITS if units is None else units),
    }


def _adjudicate(**overrides: object) -> object:
    call: dict[str, object] = {
        "run_id": "candidate",
        "champion_run_id": "champion",
        "direction": "maximize",
        "candidate_metric": _CANDIDATE,
        "champion_metric": _CHAMPION,
    }
    call.update(overrides)
    return paired_interval(_policy(), _evidence(), **call)  # type: ignore[arg-type]


def test_nested_macro_is_not_the_flat_macro_over_the_same_cells() -> None:
    flat = (0.80 + 0.50 + 0.15) / 3.0
    assert _CANDIDATE == pytest.approx(0.5625)
    assert flat == pytest.approx(0.48333333333333334)
    assert _CANDIDATE != pytest.approx(flat)
    with pytest.raises(RegistryError, match="candidate aggregate differs"):
        paired_interval(
            _policy(aggregation="macro_strata"),
            {
                **_evidence(),
                "units": [
                    {"unit_id": unit["unit_id"], "stratum": f"{unit['truth_kind']}:{unit['corpus']}",
                     "candidate": unit["candidate"], "champion": unit["champion"]}
                    for unit in _UNITS
                ],
            },
            run_id="candidate", champion_run_id="champion", direction="maximize",
            candidate_metric=_CANDIDATE, champion_metric=_CHAMPION,
        )


def test_nested_macro_adjudicates_the_registered_scalars() -> None:
    interval = _adjudicate()
    assert interval.point_estimate == pytest.approx(_CANDIDATE - _CHAMPION)
    assert interval.lower <= interval.point_estimate <= interval.upper
    assert interval.evidence["aggregation"] == "macro_truth_kind_corpus_group"
    assert interval.evidence["unit_count"] == len(_UNITS)
    assert interval.evidence["strata"] == ["derived:three", "derived:two", "direct:one"]


def test_nested_macro_refuses_a_candidate_aggregate_that_is_not_the_run_metric() -> None:
    with pytest.raises(RegistryError, match="candidate aggregate differs"):
        _adjudicate(candidate_metric=_CANDIDATE + 0.01)


def test_nested_macro_refuses_a_champion_aggregate_that_is_not_the_run_metric() -> None:
    with pytest.raises(RegistryError, match="champion aggregate differs"):
        _adjudicate(champion_metric=_CHAMPION + 0.01)


def test_nested_macro_refuses_a_single_unit_cell() -> None:
    units = [unit for unit in _UNITS if unit["unit_id"] != "v:three:g2"]
    with pytest.raises(RegistryError, match="two units per truth_kind:corpus cell"):
        paired_interval(
            _policy(), _evidence(units), run_id="candidate", champion_run_id="champion",
            direction="maximize", candidate_metric=_CANDIDATE, champion_metric=_CHAMPION,
        )


def test_nested_macro_refuses_a_unit_missing_a_macro_level() -> None:
    units = [dict(unit) for unit in _UNITS]
    del units[0]["corpus"]
    with pytest.raises(RegistryError, match="requires exactly"):
        paired_interval(
            _policy(), _evidence(units), run_id="candidate", champion_run_id="champion",
            direction="maximize", candidate_metric=_CANDIDATE, champion_metric=_CHAMPION,
        )


def test_nested_macro_minimizing_direction_flips_the_sign() -> None:
    interval = _adjudicate(direction="minimize")
    assert interval.point_estimate == pytest.approx(_CHAMPION - _CANDIDATE)
