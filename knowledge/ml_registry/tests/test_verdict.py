"""R10 acceptance: table-driven trial verdict (adopt/park/reject/void) against the
model's current baseline, the one-noise-floor and 5%-throughput/net-line boundaries, and
the 3-consecutive-distinct-idea rejection ratchet that invalidates an adoption and
restores the previous baseline without discarding the rejections that triggered it."""

from __future__ import annotations

import statistics

import pytest

from knowledge.ml_registry.floor import RATCHET_COUNT_FIELD, REJECTION_STREAK_FIELD, register_model_with_baseline
from knowledge.ml_registry.lifecycle import STATUS_PARKED, STATUS_REJECTED, STATUS_UNTRIED
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.verdict import (
    BASELINE_FIELD,
    PREVIOUS_BASELINE_FIELD,
    VERDICT_ADOPTED,
    VERDICT_PARKED,
    VERDICT_REJECTED,
    VERDICT_VOIDED,
    LedgerRow,
    adjudicate_verdict,
)
from knowledge.ml_registry.write_path import RegistrySpace, register_idea, register_trial

RUN_VALUES = [1.0, 1.02, 0.98, 1.04]
NOISE_FLOOR = statistics.stdev(RUN_VALUES)  # ~0.0258199
BASELINE_THROUGHPUT = statistics.mean(RUN_VALUES)  # 1.01

MODEL_META: dict[str, object] = {
    "metric": "val_bpb",
    "direction": "minimize",
    "win_condition": "beats baseline by noise_floor",
    "baseline": "r1",
    "diff_size_limit": 800,
    "baseline_runs": ["r1", "r2", "r3", "r4"],
}

BASELINE_LEDGER = {"r1": 1.0, "r2": 1.02, "r3": 0.98, "r4": 1.04}

# A superset of every trial commit any test below registers a trial against -- register_trial's
# own (simpler, commit-only) ledger-membership guard reads this frozenset.
ALL_COMMITS = frozenset(
    BASELINE_LEDGER.keys()
    | {
        "adopt1", "park1", "sdr1", "wr1", "boundary-park", "boundary-reject", "void1",
        "b1", "b2", "b3", "b4", "b5", "same1", "same2", "same3", "noop1", "noop2", "noop3",
        "bad-throughput", "bad-diff", "no-self-report",
    }
)


def _idea_meta(model_id, description="try RoPE scaling"):
    return {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": description}


def _space_with_model():
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), BASELINE_LEDGER)
    return space, model_id


def _trial(space, model_id, idea_id, commit, *, throughput, diff_lines):
    return register_trial(
        space,
        {
            "model_id": model_id, "idea_id": idea_id, "commit": commit, "status": "running",
            "throughput": throughput, "diff_lines": diff_lines,
        },
        ALL_COMMITS,
    )


def _rows(**by_commit: tuple[float, float, float]) -> dict[str, LedgerRow]:
    """commit -> (value, throughput, diff_lines), including the fixed baseline_runs rows so a
    baseline lookup never fails by omission."""
    rows = {
        commit: LedgerRow(value=value, throughput=BASELINE_THROUGHPUT, diff_lines=0)
        for commit, value in BASELINE_LEDGER.items()
    }
    for commit, (value, throughput, diff_lines) in by_commit.items():
        rows[commit] = LedgerRow(value=value, throughput=throughput, diff_lines=diff_lines)
    return rows


# ---------------------------------------------------------------------------
# The four verdict rows
# ---------------------------------------------------------------------------


def test_adopt_beyond_one_noise_floor_in_the_improving_direction():
    space, model_id = _space_with_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "adopt1", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    ledger = _rows(adopt1=(1.0 - NOISE_FLOOR - 0.01, BASELINE_THROUGHPUT, 100))

    verdict = adjudicate_verdict(space, trial_id, ledger)

    assert verdict == VERDICT_ADOPTED
    assert space.get(trial_id).meta["status"] == "succeeded"
    assert space.get(idea_id).meta["status"] == "adopted"
    model = space.get(model_id)
    assert model.meta[BASELINE_FIELD] == "adopt1"
    assert model.meta[PREVIOUS_BASELINE_FIELD] == "r1"
    assert model.meta[RATCHET_COUNT_FIELD] == 0
    assert model.meta[REJECTION_STREAK_FIELD] == []


def test_park_when_stagnant_and_within_both_bounds():
    space, model_id = _space_with_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "park1", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    ledger = _rows(park1=(1.0, BASELINE_THROUGHPUT, 100))  # delta == 0, well within the floor

    verdict = adjudicate_verdict(space, trial_id, ledger)

    assert verdict == VERDICT_PARKED
    assert space.get(trial_id).meta["status"] == "stagnant"
    assert space.get(idea_id).meta["status"] == STATUS_PARKED


def test_reject_when_stagnant_but_breaching_the_net_line_bound():
    space, model_id = _space_with_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "sdr1", throughput=BASELINE_THROUGHPUT, diff_lines=900)
    ledger = _rows(sdr1=(1.0, BASELINE_THROUGHPUT, 900))  # stagnant, diff_lines > diff_size_limit=800

    verdict = adjudicate_verdict(space, trial_id, ledger)

    assert verdict == VERDICT_REJECTED
    assert space.get(idea_id).meta["status"] == STATUS_REJECTED
    model = space.get(model_id)
    # a stagnant/diff-bound rejection does NOT advance the worsening-direction ratchet
    assert model.meta[RATCHET_COUNT_FIELD] == 0


def test_reject_beyond_one_noise_floor_in_the_worsening_direction():
    space, model_id = _space_with_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "wr1", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    ledger = _rows(wr1=(1.0 + NOISE_FLOOR + 0.01, BASELINE_THROUGHPUT, 100))

    verdict = adjudicate_verdict(space, trial_id, ledger)

    assert verdict == VERDICT_REJECTED
    assert space.get(idea_id).meta["status"] == STATUS_REJECTED
    model = space.get(model_id)
    assert model.meta[RATCHET_COUNT_FIELD] == 1
    assert model.meta[REJECTION_STREAK_FIELD] == [idea_id]


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


def test_delta_exactly_one_noise_floor_improving_is_stagnant_not_adopted():
    space, model_id = _space_with_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "boundary-park", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    ledger = _rows(**{"boundary-park": (1.0 - NOISE_FLOOR, BASELINE_THROUGHPUT, 100)})  # delta == noise_floor exactly

    verdict = adjudicate_verdict(space, trial_id, ledger)

    assert verdict == VERDICT_PARKED  # "within" one std dev is stagnant, "beyond" (strict) adopts


def test_delta_at_least_one_noise_floor_worsening_rejects():
    space, model_id = _space_with_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "boundary-reject", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    # a hair past -noise_floor (float subtraction of the exact boundary is not bit-stable) --
    # still exercises the "at least" (<=) inclusive edge, not just a comfortable margin.
    ledger = _rows(**{"boundary-reject": (1.0 + NOISE_FLOOR + 1e-9, BASELINE_THROUGHPUT, 100)})

    verdict = adjudicate_verdict(space, trial_id, ledger)

    assert verdict == VERDICT_REJECTED  # "at least" one std dev below baseline rejects


def test_throughput_more_than_5_percent_below_baseline_is_voided_not_adjudicated():
    space, model_id = _space_with_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    low_throughput = BASELINE_THROUGHPUT * 0.94  # > 5% below
    trial_id = _trial(space, model_id, idea_id, "void1", throughput=low_throughput, diff_lines=100)
    # even a big improving delta must not be adjudicated once voided
    ledger = _rows(void1=(1.0 - 10 * NOISE_FLOOR, low_throughput, 100))

    verdict = adjudicate_verdict(space, trial_id, ledger)

    assert verdict == VERDICT_VOIDED
    assert space.get(trial_id).meta["status"] == "voided"
    assert space.get(idea_id).meta.get("status", STATUS_UNTRIED) == STATUS_UNTRIED  # no adjudication happened
    model = space.get(model_id)
    assert model.meta[BASELINE_FIELD] == "r1"  # baseline never moved


def test_throughput_exactly_5_percent_below_baseline_is_not_voided():
    space, model_id = _space_with_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    boundary_throughput = BASELINE_THROUGHPUT * 0.95
    trial_id = _trial(space, model_id, idea_id, "park1", throughput=boundary_throughput, diff_lines=100)
    ledger = _rows(park1=(1.0, boundary_throughput, 100))

    verdict = adjudicate_verdict(space, trial_id, ledger)

    assert verdict != VERDICT_VOIDED
    assert verdict == VERDICT_PARKED


# ---------------------------------------------------------------------------
# Ratchet: 3 consecutive rejecting trials on distinct ideas
# ---------------------------------------------------------------------------


def test_ratchet_fires_on_the_third_consecutive_worsening_reject_on_a_distinct_idea():
    space, model_id = _space_with_model()

    winner_id = register_idea(space, _idea_meta(model_id, "winner"))
    winner_trial = _trial(space, model_id, winner_id, "adopt1", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    adjudicate_verdict(space, winner_trial, _rows(adopt1=(1.0 - NOISE_FLOOR - 0.01, BASELINE_THROUGHPUT, 100)))
    model = space.get(model_id)
    assert model.meta[BASELINE_FIELD] == "adopt1"
    assert model.meta[PREVIOUS_BASELINE_FIELD] == "r1"

    loser_ids = [register_idea(space, _idea_meta(model_id, f"loser-{i}")) for i in range(3)]
    worse_value = 1.0 + 10 * NOISE_FLOOR  # far worse than the new baseline, either way
    for i, (loser_id, commit) in enumerate(zip(loser_ids, ["b1", "b2", "b3"])):
        ledger = _rows(**{commit: (worse_value, BASELINE_THROUGHPUT, 100), "adopt1": (1.0 - NOISE_FLOOR - 0.01, BASELINE_THROUGHPUT, 100)})
        trial_id = _trial(space, model_id, loser_id, commit, throughput=BASELINE_THROUGHPUT, diff_lines=100)
        verdict = adjudicate_verdict(space, trial_id, ledger)
        assert verdict == VERDICT_REJECTED

    model = space.get(model_id)
    winner = space.get(winner_id)
    assert winner.meta["status"] == STATUS_UNTRIED  # adoption invalidated
    assert "ratchet" in winner.meta["reversal_reason"]
    assert model.meta[BASELINE_FIELD] == "r1"  # restored to the previous baseline
    assert PREVIOUS_BASELINE_FIELD not in model.meta
    assert model.meta[RATCHET_COUNT_FIELD] == 0
    assert model.meta[REJECTION_STREAK_FIELD] == []
    # the 3 rejections themselves are NOT discarded/re-queued
    for loser_id in loser_ids:
        assert space.get(loser_id).meta["status"] == STATUS_REJECTED


def test_ratchet_does_not_fire_on_the_same_idea_rejected_repeatedly():
    space, model_id = _space_with_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    worse_value = 1.0 + 10 * NOISE_FLOOR

    for commit in ["same1", "same2", "same3"]:
        trial_id = _trial(space, model_id, idea_id, commit, throughput=BASELINE_THROUGHPUT, diff_lines=100)
        ledger = _rows(**{commit: (worse_value, BASELINE_THROUGHPUT, 100)})
        verdict = adjudicate_verdict(space, trial_id, ledger)
        assert verdict == VERDICT_REJECTED

    model = space.get(model_id)
    assert model.meta[RATCHET_COUNT_FIELD] == 3  # counted, but never 3 DISTINCT in a row
    assert model.meta[REJECTION_STREAK_FIELD] == [idea_id, idea_id, idea_id]


def test_ratchet_with_no_prior_adoption_is_a_no_op_that_only_resets_the_counter():
    space, model_id = _space_with_model()
    loser_ids = [register_idea(space, _idea_meta(model_id, f"noop-{i}")) for i in range(3)]
    worse_value = 1.0 + 10 * NOISE_FLOOR

    for loser_id, commit in zip(loser_ids, ["noop1", "noop2", "noop3"]):
        trial_id = _trial(space, model_id, loser_id, commit, throughput=BASELINE_THROUGHPUT, diff_lines=100)
        ledger = _rows(**{commit: (worse_value, BASELINE_THROUGHPUT, 100)})
        verdict = adjudicate_verdict(space, trial_id, ledger)  # must not raise despite no active adoption
        assert verdict == VERDICT_REJECTED

    model = space.get(model_id)
    assert model.meta[RATCHET_COUNT_FIELD] == 0
    assert model.meta[REJECTION_STREAK_FIELD] == []
    for loser_id in loser_ids:
        assert space.get(loser_id).meta["status"] == STATUS_REJECTED


# ---------------------------------------------------------------------------
# Self-report vs ledger-recomputation refusals
# ---------------------------------------------------------------------------


def test_refuses_a_self_reported_throughput_that_disagrees_with_the_ledger_recomputation():
    space, model_id = _space_with_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "bad-throughput", throughput=999.0, diff_lines=100)
    ledger = _rows(**{"bad-throughput": (1.0, BASELINE_THROUGHPUT, 100)})

    with pytest.raises(RegistryValidationError) as excinfo:
        adjudicate_verdict(space, trial_id, ledger)
    assert excinfo.value.field == "throughput"


def test_refuses_a_self_reported_diff_lines_that_disagrees_with_the_ledger_recomputation():
    space, model_id = _space_with_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "bad-diff", throughput=BASELINE_THROUGHPUT, diff_lines=999)
    ledger = _rows(**{"bad-diff": (1.0, BASELINE_THROUGHPUT, 100)})

    with pytest.raises(RegistryValidationError) as excinfo:
        adjudicate_verdict(space, trial_id, ledger)
    assert excinfo.value.field == "diff_lines"


def test_refuses_a_trial_missing_a_self_reported_field():
    space, model_id = _space_with_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = register_trial(
        space, {"model_id": model_id, "idea_id": idea_id, "commit": "no-self-report", "status": "running"},
        ALL_COMMITS,
    )
    ledger = _rows(**{"no-self-report": (1.0, BASELINE_THROUGHPUT, 100)})

    with pytest.raises(RegistryValidationError) as excinfo:
        adjudicate_verdict(space, trial_id, ledger)
    assert excinfo.value.field == "throughput"


def test_refuses_a_trial_commit_missing_from_the_ledger():
    space, model_id = _space_with_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "adopt1", throughput=BASELINE_THROUGHPUT, diff_lines=100)

    with pytest.raises(RegistryValidationError) as excinfo:
        adjudicate_verdict(space, trial_id, {})
    assert excinfo.value.field == "commit"


def test_refuses_when_the_models_baseline_commit_has_no_ledger_row():
    space, model_id = _space_with_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "adopt1", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    ledger = {"adopt1": LedgerRow(value=1.0, throughput=BASELINE_THROUGHPUT, diff_lines=100)}  # no "r1" row

    with pytest.raises(RegistryValidationError) as excinfo:
        adjudicate_verdict(space, trial_id, ledger)
    assert excinfo.value.field == "baseline"
