"""A noise floor that SHRINKS as the campaign approaches its ceiling.

Pins both directions of the shape, the armor that stops it falling to zero, that
court_marking (0.155648 -> 0.70, the case the binomial form INVERTS) shrinks like the
rest, that a model declaring nothing is bit-for-bit untouched, and that the ratchet's
counterfactual asks its question with the PREVIOUS era's bar rather than the current one.
"""

from __future__ import annotations

import math
import statistics

import pytest

from knowledge.ml_registry.floor import (
    DEFAULT_FLOOR_ARMOR,
    FLOOR_ARMOR_FIELD,
    FLOOR_MEASURED_AT_FIELD,
    FLOOR_SCALING_BASIS_FIELD,
    FLOOR_SCALING_BASIS_STATIC,
    FLOOR_SCALING_BASIS_UNVERIFIED_MODEL,
    FLOOR_SCALING_FIELD,
    FLOOR_SCALING_RESIDUAL,
    METRIC_CEILING_FIELD,
    NOISE_FLOOR_FIELD,
    RATCHET_COUNT_FIELD,
    describe_noise_floor,
    register_model_with_baseline,
    scaled_noise_floor,
)
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.verdict import (
    PREVIOUS_BASELINE_FIELD,
    VERDICT_ADOPTED,
    VERDICT_REJECTED,
    LedgerRow,
    adjudicate_verdict,
)
from knowledge.ml_registry.write_path import RegistrySpace, register_idea, register_trial

# ---------------------------------------------------------------------------
# The four live campaigns, at their registered baseline and at their win condition.
# (campaign, direction, ceiling, v_measured, registered floor, win condition)
# ---------------------------------------------------------------------------
LIVE_CAMPAIGNS = [
    ("detection", "maximize", 1.0, 0.6076, 0.099758, 0.80),
    ("association", "maximize", 1.0, 0.851439, 0.0016, 0.90),
    ("contact_point", "minimize", 0.0, 0.188458, 0.0041, 0.10),
    ("court_marking", "maximize", 1.0, 0.155648, 0.024962, 0.70),
]


def _scaled_meta(direction, ceiling, measured_at, floor, **extra):
    return {
        "direction": direction,
        NOISE_FLOOR_FIELD: floor,
        METRIC_CEILING_FIELD: ceiling,
        FLOOR_MEASURED_AT_FIELD: measured_at,
        FLOOR_SCALING_FIELD: FLOOR_SCALING_RESIDUAL,
        **extra,
    }


@pytest.mark.parametrize("name,direction,ceiling,measured_at,floor,win", LIVE_CAMPAIGNS)
def test_every_live_campaign_shrinks_monotonically_toward_its_ceiling(
    name, direction, ceiling, measured_at, floor, win
):
    meta = _scaled_meta(direction, ceiling, measured_at, floor)
    steps = [measured_at + (win - measured_at) * i / 200 for i in range(201)]
    bars = [scaled_noise_floor(meta, v) for v in steps]
    assert bars[0] == pytest.approx(floor), f"{name} starts at its registered floor"
    assert bars[-1] < bars[0], f"{name} bar must SHRINK from baseline to win, not grow"
    for earlier, later in zip(bars, bars[1:]):
        assert later <= earlier + 1e-15, f"{name} bar rose while the metric improved"


def test_court_marking_does_not_invert_where_the_binomial_form_would():
    """p(1-p) peaks at 0.5, so sqrt(p(1-p)/n) RAISES the bar for a campaign climbing
    through the low half -- 26% higher at court_marking's win condition than at its
    baseline. Residual-to-ceiling is monotone over the whole range."""
    def binomial(p: float) -> float:
        return math.sqrt(p * (1 - p) / 429)
    assert binomial(0.70) / binomial(0.155648) > 1.25  # the defect, reproduced

    meta = _scaled_meta("maximize", 1.0, 0.155648, 0.024962)
    assert scaled_noise_floor(meta, 0.70) < scaled_noise_floor(meta, 0.155648)


def test_minimize_metric_shrinks_toward_a_ceiling_of_zero():
    meta = _scaled_meta("minimize", 0.0, 0.20, 0.004)
    assert scaled_noise_floor(meta, 0.20) == pytest.approx(0.004)
    assert scaled_noise_floor(meta, 0.10) == pytest.approx(0.002)
    # ...and rises back toward -- but never above -- the registered floor if the campaign
    # moves AWAY from the ceiling.
    assert scaled_noise_floor(meta, 0.40) == pytest.approx(0.004)


def test_armor_stops_the_bar_at_a_fraction_of_the_measured_floor():
    """A pure relative-error floor has no lower bound: association at HOTA 0.99 scales to
    ~0.0002, below the 0.000790 SD of a perturbation whose true effect is ZERO."""
    meta = _scaled_meta("maximize", 1.0, 0.851439, 0.0016)
    unarmored = 0.0016 * (0.01 / (1.0 - 0.851439))
    assert unarmored < 0.000790  # the defect, reproduced

    bar = scaled_noise_floor(meta, 0.99)
    assert bar == pytest.approx(0.0016 * DEFAULT_FLOOR_ARMOR)
    assert bar > 0.000790  # above the one measured null this registry holds
    assert describe_noise_floor(meta, 0.99)["armored"] is True
    # and it never goes lower, however close to the ceiling the campaign gets
    assert scaled_noise_floor(meta, 1.0) == pytest.approx(0.0016 * DEFAULT_FLOOR_ARMOR)
    assert scaled_noise_floor(meta, 1.5) == pytest.approx(0.0016 * DEFAULT_FLOOR_ARMOR)


def test_armor_is_overridable_per_model():
    meta = _scaled_meta("maximize", 1.0, 0.851439, 0.0016, **{FLOOR_ARMOR_FIELD: 0.8})
    assert scaled_noise_floor(meta, 0.99) == pytest.approx(0.0016 * 0.8)


@pytest.mark.parametrize("name,direction,ceiling,measured_at,floor,win", LIVE_CAMPAIGNS)
def test_derived_bar_never_leaves_the_registered_floors_neighbourhood(
    name, direction, ceiling, measured_at, floor, win
):
    """The magnitude guarantee. _check_floor_against_spread bounds the REGISTERED floor
    against the spread real ledger rows show; it can say nothing about a metric level
    nobody has run. So the derived bar is clamped to [armor x registered, registered] and
    inherits that bound instead of escaping it."""
    meta = _scaled_meta(direction, ceiling, measured_at, floor)
    span = [-10.0, -1.0, 0.0, measured_at, win, ceiling, ceiling + 5.0, 1e6]
    for v in span:
        bar = scaled_noise_floor(meta, v)
        assert floor * DEFAULT_FLOOR_ARMOR - 1e-15 <= bar <= floor + 1e-15, (name, v, bar)


# ---------------------------------------------------------------------------
# BACKWARD COMPATIBILITY: a model that declares nothing keeps a static absolute floor.
# ---------------------------------------------------------------------------

UNDECLARED_META: dict[str, object] = {
    "metric": "val_bpb",
    "direction": "minimize",
    "win_condition": {"metric_at_most": 0.5},
    "baseline": "r1",
    "diff_size_limit": 800,
    "baseline_runs": ["r1", "r2", "r3", "r4"],
}
BASELINE_LEDGER = {"r1": 1.0, "r2": 1.02, "r3": 0.98, "r4": 1.04}
STATIC_FLOOR = statistics.stdev(BASELINE_LEDGER.values())


@pytest.mark.parametrize("at", [0.0, 0.5, 1.0, 1.5, 100.0])
def test_a_model_that_declares_nothing_gets_the_registered_floor_at_every_level(at):
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(UNDECLARED_META), BASELINE_LEDGER)
    meta = space.get(model_id).meta
    assert meta[FLOOR_SCALING_BASIS_FIELD] == FLOOR_SCALING_BASIS_STATIC
    assert FLOOR_MEASURED_AT_FIELD not in meta  # nothing stamped that nobody asked for
    assert scaled_noise_floor(meta, at) == meta[NOISE_FLOOR_FIELD] == pytest.approx(STATIC_FLOOR)


@pytest.mark.parametrize("name,direction,ceiling,measured_at,floor,win", LIVE_CAMPAIGNS)
def test_a_live_campaign_that_does_not_opt_in_is_untouched(
    name, direction, ceiling, measured_at, floor, win
):
    static = {"direction": direction, NOISE_FLOOR_FIELD: floor}
    assert scaled_noise_floor(static, measured_at) == floor
    assert scaled_noise_floor(static, win) == floor
    assert describe_noise_floor(static, win)[FLOOR_SCALING_FIELD] == "static"


# ---------------------------------------------------------------------------
# REGISTRATION: what the registry can check about a moving bar, and what it labels.
# ---------------------------------------------------------------------------


def _register_scaled(**overrides):
    space = RegistrySpace()
    meta = dict(
        UNDECLARED_META,
        direction="maximize",
        baseline="b1",
        baseline_runs=["b1", "b2", "b3", "b4"],
        win_condition={"metric_at_least": 0.9},
        void_throughput_fraction=0.0,
        **{FLOOR_SCALING_FIELD: FLOOR_SCALING_RESIDUAL, METRIC_CEILING_FIELD: 1.0},
    )
    meta.update(overrides)
    ledger = {"b1": 0.50, "b2": 0.52, "b3": 0.48, "b4": 0.54}
    return space, register_model_with_baseline(space, meta, ledger), ledger


def test_registration_stamps_the_level_the_floor_was_measured_at_and_labels_what_it_checked():
    space, model_id, ledger = _register_scaled()
    meta = space.get(model_id).meta
    assert meta[FLOOR_MEASURED_AT_FIELD] == pytest.approx(statistics.mean(ledger.values()))
    # The shape was checked; the PROPORTIONALITY was not and cannot be -- praxis holds one
    # noise measurement, at one metric level. The stamp says so rather than staying silent.
    assert meta[FLOOR_SCALING_BASIS_FIELD] == FLOOR_SCALING_BASIS_UNVERIFIED_MODEL


@pytest.mark.parametrize(
    "overrides,field",
    [
        ({FLOOR_SCALING_FIELD: "shrinking"}, FLOOR_SCALING_FIELD),
        ({METRIC_CEILING_FIELD: None}, METRIC_CEILING_FIELD),
        ({METRIC_CEILING_FIELD: 0.3}, METRIC_CEILING_FIELD),  # already past it
        ({FLOOR_ARMOR_FIELD: 0.0}, FLOOR_ARMOR_FIELD),
        ({FLOOR_ARMOR_FIELD: 1.5}, FLOOR_ARMOR_FIELD),
        ({FLOOR_ARMOR_FIELD: "half"}, FLOOR_ARMOR_FIELD),
    ],
)
def test_a_half_declared_scaling_is_refused_naming_the_field(overrides, field):
    with pytest.raises(RegistryValidationError) as excinfo:
        _register_scaled(**overrides)
    assert excinfo.value.field == field


def test_a_scaling_declared_where_no_ledger_can_supply_the_measured_level_is_refused():
    from knowledge.ml_registry.write_path import register_model

    space = RegistrySpace()
    meta = dict(
        UNDECLARED_META,
        direction="maximize",
        noise_floor=0.02,
        baseline_throughput=0.5,
        **{FLOOR_SCALING_FIELD: FLOOR_SCALING_RESIDUAL, METRIC_CEILING_FIELD: 1.0},
    )
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model(space, meta)
    assert excinfo.value.field == FLOOR_MEASURED_AT_FIELD


# ---------------------------------------------------------------------------
# D3: the ratchet's counterfactual under a bar that MOVES.
# ---------------------------------------------------------------------------

ALL_COMMITS = frozenset({"b1", "b2", "b3", "b4", "adopt1", "loss1"})


def _rows(ledger, **extra):
    rows = {c: LedgerRow(value=v, throughput=1.0, diff_lines=0) for c, v in ledger.items()}
    for commit, value in extra.items():
        rows[commit] = LedgerRow(value=value, throughput=1.0, diff_lines=1)
    return rows


def test_the_ratchet_counterfactual_uses_the_bar_of_the_era_it_asks_about():
    """The question is "would this trial have PARKED OR WON against the bar the adoption
    REPLACED" -- so it must be asked with the floor that stood at previous_baseline's
    level. Under a moving bar the current floor is a different (smaller, because the
    adoption improved the metric) number, and using it errs toward UNDER-firing: the
    rejection is filed unattributable, the streak never starts, and a false adoption
    survives longer exactly when the looser bar is creating more of them.
    """
    space, model_id, ledger = _register_scaled()
    floor = space.get(model_id).meta[NOISE_FLOOR_FIELD]
    measured_at = space.get(model_id).meta[FLOOR_MEASURED_AT_FIELD]

    idea1 = register_idea(space, {"model_id": model_id, "origin": "seeded", "axis": "a", "description": "one"})
    trial1 = register_trial(
        space,
        {"model_id": model_id, "idea_id": idea1, "commit": "adopt1", "status": "running",
         "throughput": 1.0, "diff_lines": 1},
        ALL_COMMITS,
    )
    assert adjudicate_verdict(space, trial1, _rows(ledger, adopt1=0.80)) == VERDICT_ADOPTED
    assert space.get(model_id).meta[PREVIOUS_BASELINE_FIELD] == "b1"

    bar_now = scaled_noise_floor(space.get(model_id).meta, 0.80)
    bar_then = scaled_noise_floor(space.get(model_id).meta, 0.50)
    assert bar_now < bar_then  # the bar really did move; otherwise this test proves nothing

    # A loss that rejects against the NEW baseline (0.80) and would merely have PARKED
    # against the OLD one (0.50) -- attributable to the adoption, and ONLY under the old
    # era's bar: 0.48 is 0.02 below 0.50, inside `bar_then` and outside `bar_now`.
    assert bar_now < 0.02 < bar_then
    idea2 = register_idea(space, {"model_id": model_id, "origin": "seeded", "axis": "b", "description": "two"})
    trial2 = register_trial(
        space,
        {"model_id": model_id, "idea_id": idea2, "commit": "loss1", "status": "running",
         "throughput": 1.0, "diff_lines": 1},
        ALL_COMMITS,
    )
    assert adjudicate_verdict(space, trial2, _rows(ledger, adopt1=0.80, loss1=0.48)) == VERDICT_REJECTED
    assert space.get(model_id).meta[RATCHET_COUNT_FIELD] == 1, (
        "the rejection is attributable under the previous era's bar and must join the streak; "
        "counting it with the CURRENT bar would leave the ratchet at 0"
    )
    assert floor > 0 and measured_at > 0


DETECTION_BASELINE_LEDGER = {
    "d0": 0.6925, "d1": 0.6164, "d2": 0.6076, "d3": 0.6178,
    "d4": 0.5895, "d5": 0.5355, "d7": 0.5560, "d8": 0.6505,
}


def test_the_detection_campaign_registers_and_adjudicates_exactly_as_before():
    """The live record, unchanged: a declared bootstrap floor, no scaling declaration. It
    must register to the same 0.099758 and adjudicate its best recorded arm (clahe 0.6203,
    +0.0127 over the registered baseline) as the same non-adoption it is today."""
    from knowledge.ml_registry.floor import adjudicate_trial

    space = RegistrySpace()
    meta = dict(
        UNDECLARED_META,
        metric="tiny_person_recall_at_p90",
        direction="maximize",
        win_condition={"metric_at_least": 0.80},
        baseline="d2",
        baseline_runs=list(DETECTION_BASELINE_LEDGER),
        noise_floor=0.099758,
        noise_floor_method="bootstrap",
        noise_floor_varies="paired_delta",
        trial_comparison="paired",
    )
    ledger = dict(DETECTION_BASELINE_LEDGER, clahe=0.6203)
    model_id = register_model_with_baseline(space, meta, ledger)
    stored = space.get(model_id).meta
    assert stored[NOISE_FLOOR_FIELD] == 0.099758
    assert stored[FLOOR_SCALING_BASIS_FIELD] == FLOOR_SCALING_BASIS_STATIC
    assert scaled_noise_floor(stored, 0.6076) == 0.099758

    idea_id = register_idea(space, {"model_id": model_id, "origin": "seeded", "axis": "a", "description": "clahe"})
    trial_id = register_trial(
        space,
        {"model_id": model_id, "idea_id": idea_id, "commit": "clahe", "status": "running"},
        frozenset(ledger),
    )
    assert adjudicate_trial(space, trial_id, ledger) == "failed"
