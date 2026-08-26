"""Regression coverage for the frozen no-floor paired-interval judge."""

from __future__ import annotations

import pytest

from knowledge.ml_registry.services.paired_adjudication import paired_interval
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
