"""R10 acceptance: table-driven trial verdict (adopt/park/reject/void) against the
model's current baseline, the symmetric one-noise-floor and 5%-throughput/net-line
boundaries, the supersession of a prior adoption by a better one (which preserves that
adoption's rejections), and the 3-consecutive-distinct-idea rejection ratchet that
invalidates an adoption, restores the previous baseline, and re-queues every idea rejected
under the false baseline it set."""

from __future__ import annotations

import statistics

import pytest

from knowledge.ml_registry.floor import (
    BASELINE_THROUGHPUT_UNITS_FIELD,
    RATCHET_COUNT_FIELD,
    REJECTION_STREAK_FIELD,
    THROUGHPUT_UNITS_METRIC_MEAN,
    THROUGHPUT_UNITS_ROWS_PER_SEC,
    register_model_with_baseline,
)
from knowledge.ml_registry.lifecycle import (
    STATUS_ADOPTED,
    STATUS_PARKED,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
    STATUS_UNTRIED,
    reject_idea,
    rejection_memory,
    untried_backlog,
)
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.verdict import (
    BASELINE_FIELD,
    PREVIOUS_BASELINE_FIELD,
    VERDICT_ADOPTED,
    VERDICT_PARKED,
    VERDICT_REJECTED,
    VERDICT_VOIDED,
    RATCHET_STREAK_LENGTH,
    LedgerRow,
    adjudicate_verdict,
)
from knowledge.ml_registry.write_path import RegistrySpace, register_idea, register_model, register_trial

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
        "win1", "win2", "exact-improving", "exact-worsening",
        "ax1", "ax2", "ax3", "sameax1", "sameax2", "sameax3",
        "bad1", "bad2", "bad3", "bad4", "bad5", "bad6",
        "mix1", "mix2", "mix3", "mix4",
        "wide1", "wide2", "wide3", "deep1", "deep2", "deep3", "ghost-loser", "slow1",
        "ordinary1", "ordinary2", "ordinary3", "inherit1", "inherit2", "inherit3",
    }
)

# The value an adopting trial scores: a win of one floor plus a hair over the r1 baseline of 1.0.
ADOPTED_VALUE = 1.0 - NOISE_FLOOR - 0.01

# A rejection ATTRIBUTABLE to that adoption: more than one floor WORSE than the adopted
# baseline (so it rejects), but level with the pre-adoption baseline r1=1.0, i.e. it would
# merely have PARKED against the bar the adoption replaced. Its loss is explicable only by the
# raised bar, which is the whole of what the ratchet claims to infer.
ATTRIBUTABLE_LOSS = 1.0

# A rejection that says NOTHING about the adoption: 10 floors worse than the pre-adoption
# baseline too, so it would have been rejected just as hard before the adoption existed.
UNATTRIBUTABLE_LOSS = 1.0 + 10 * NOISE_FLOOR

# A second model whose noise floor is EXACTLY representable in binary floating point, so a
# delta of exactly +/- one floor can be constructed bit-for-bit rather than a hair off it.
# Three equal runs plus one differing by d give stdev == d/2: d = 0.25 -> floor == 0.125.
EXACT_LEDGER = {"e1": 1.0, "e2": 1.0, "e3": 1.0, "e4": 1.25}
EXACT_FLOOR = 0.125
EXACT_THROUGHPUT = 1.0625


def _space_with_exact_floor_model():
    space = RegistrySpace()
    meta = dict(MODEL_META, baseline="e1", baseline_runs=["e1", "e2", "e3", "e4"])
    model_id = register_model_with_baseline(
        space, meta, EXACT_LEDGER,
        ledger_throughputs={commit: EXACT_THROUGHPUT for commit in EXACT_LEDGER},
    )
    assert space.get(model_id).meta["noise_floor"] == EXACT_FLOOR  # exact, not merely close
    return space, model_id


def _exact_rows(commit: str, value: float) -> dict[str, LedgerRow]:
    rows = {
        c: LedgerRow(value=v, throughput=EXACT_THROUGHPUT, diff_lines=0)
        for c, v in EXACT_LEDGER.items()
    }
    rows[commit] = LedgerRow(value=value, throughput=EXACT_THROUGHPUT, diff_lines=100)
    return rows


def _idea_meta(model_id, description="try RoPE scaling", axis="architecture"):
    return {"model_id": model_id, "origin": "seeded", "axis": axis, "description": description}


def _space_with_model():
    """A model whose baseline_throughput is a REAL rows/sec bar, stamped as one.

    Registering without ledger throughputs stores the mean of the baseline METRIC values and
    stamps it ``metric_mean``, which the speed void now refuses to fire against -- so a fixture
    for a model that HAS a speed gate has to be registered from throughputs. The throughputs are
    all BASELINE_THROUGHPUT so the stored bar (their minimum) is the same number every test
    below was already written against.
    """
    space = RegistrySpace()
    model_id = register_model_with_baseline(
        space, dict(MODEL_META), BASELINE_LEDGER,
        ledger_throughputs={commit: BASELINE_THROUGHPUT for commit in BASELINE_LEDGER},
    )
    assert space.get(model_id).meta[BASELINE_THROUGHPUT_UNITS_FIELD] == THROUGHPUT_UNITS_ROWS_PER_SEC
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


def test_delta_beyond_one_noise_floor_worsening_rejects():
    space, model_id = _space_with_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "boundary-reject", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    ledger = _rows(**{"boundary-reject": (1.0 + NOISE_FLOOR + 0.01, BASELINE_THROUGHPUT, 100)})

    verdict = adjudicate_verdict(space, trial_id, ledger)

    assert verdict == VERDICT_REJECTED  # MORE than one std dev below baseline rejects


# The stagnant band must be closed on BOTH sides: a delta of exactly one noise floor is one
# standard deviation, i.e. no evidence, whichever direction it points. These two use the
# exact-floor model so the delta really is +/- the floor bit-for-bit, not a hair off it.


def test_delta_exactly_one_noise_floor_improving_is_stagnant():
    space, model_id = _space_with_exact_floor_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "exact-improving", throughput=EXACT_THROUGHPUT, diff_lines=100)

    verdict = adjudicate_verdict(space, trial_id, _exact_rows("exact-improving", 1.0 - EXACT_FLOOR))

    assert verdict == VERDICT_PARKED
    assert space.get(idea_id).meta["status"] == STATUS_PARKED


def test_delta_exactly_one_noise_floor_worsening_is_stagnant_too_not_rejected():
    """Bug 3 regression: the boundary used to be asymmetric -- exactly +1sd was stagnant
    while exactly -1sd rejected, so the same amount of evidence was read two ways."""
    space, model_id = _space_with_exact_floor_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "exact-worsening", throughput=EXACT_THROUGHPUT, diff_lines=100)

    verdict = adjudicate_verdict(space, trial_id, _exact_rows("exact-worsening", 1.0 + EXACT_FLOOR))

    assert verdict == VERDICT_PARKED
    assert space.get(idea_id).meta["status"] == STATUS_PARKED
    assert space.get(model_id).meta[RATCHET_COUNT_FIELD] == 0  # and no ratchet advance


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


def test_void_throughput_fraction_zero_skips_the_void_gate():
    """CV campaigns disable VOID rather than hacking baseline_throughput=0.01."""
    space, model_id = _space_with_model()
    space.get(model_id).meta["void_throughput_fraction"] = 0
    idea_id = register_idea(space, _idea_meta(model_id))
    low_throughput = BASELINE_THROUGHPUT * 0.90
    trial_id = _trial(space, model_id, idea_id, "void1", throughput=low_throughput, diff_lines=100)
    ledger = _rows(void1=(1.0 - 10 * NOISE_FLOOR, low_throughput, 100))

    verdict = adjudicate_verdict(space, trial_id, ledger)

    assert verdict != VERDICT_VOIDED
    assert verdict == VERDICT_ADOPTED


def test_throughput_void_records_a_reason_so_it_is_not_confused_with_an_unfair_run():
    space, model_id = _space_with_model()
    idea_id = register_idea(space, _idea_meta(model_id))
    low_throughput = BASELINE_THROUGHPUT * 0.94
    trial_id = _trial(space, model_id, idea_id, "void1", throughput=low_throughput, diff_lines=100)
    ledger = _rows(void1=(1.0 - 10 * NOISE_FLOOR, low_throughput, 100))

    assert adjudicate_verdict(space, trial_id, ledger) == VERDICT_VOIDED
    reason = space.get(trial_id).meta["void_reason"]
    assert "throughput" in reason
    assert "budget_exhausted" not in reason


def test_void_throughput_fraction_zero_does_not_disable_unfair_run_voids():
    """#32's unfair-run void is a different gate. Disabling speed VOID must not adjudicate
    a budget_exhausted arm -- that would re-introduce the truncated-graph-model rejection."""
    space, model_id = _space_with_model()
    space.get(model_id).meta["void_throughput_fraction"] = 0
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "void1",
                      throughput=BASELINE_THROUGHPUT, diff_lines=100)
    ledger = _rows(void1=(1.0 - 10 * NOISE_FLOOR, BASELINE_THROUGHPUT, 100))
    ledger["void1"] = LedgerRow(
        value=1.0 - 10 * NOISE_FLOOR, throughput=BASELINE_THROUGHPUT, diff_lines=100,
        status="budget_exhausted",
    )

    assert adjudicate_verdict(space, trial_id, ledger) == VERDICT_VOIDED
    assert "budget_exhausted" in space.get(trial_id).meta["void_reason"]


def test_explicit_void_throughput_fraction_matches_the_default():
    space, model_id = _space_with_model()
    space.get(model_id).meta["void_throughput_fraction"] = 0.05
    idea_id = register_idea(space, _idea_meta(model_id))
    low_throughput = BASELINE_THROUGHPUT * 0.94
    trial_id = _trial(space, model_id, idea_id, "void1", throughput=low_throughput, diff_lines=100)
    ledger = _rows(void1=(1.0 - 10 * NOISE_FLOOR, low_throughput, 100))

    assert adjudicate_verdict(space, trial_id, ledger) == VERDICT_VOIDED


# ---------------------------------------------------------------------------
# The speed void reads the UNITS the throughput bar was stamped with
# ---------------------------------------------------------------------------


def test_the_speed_void_refuses_a_metric_mean_bar_instead_of_gating_speed_on_it():
    """Mirror of floor.adjudicate_trial's rows-per-sec refusal, running the other way.

    ``baseline_throughput`` holds the mean of the baseline METRIC values when registration was
    given no throughputs, and registration stamps it so. Reproduced against the unguarded gate:
    a metric_mean-stamped model with a mean of 0.90 and a real ledger throughput of 0.5 rows/sec
    voided every trial -- "throughput 0.5 is more than 5% below baseline_throughput 0.9" -- until
    the void limit closed the campaign. A metric mean cannot bound a speed, so there is no gate
    to run here and the honest answer is to say so.
    """
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), BASELINE_LEDGER)
    model = space.get(model_id)
    assert model.meta[BASELINE_THROUGHPUT_UNITS_FIELD] == THROUGHPUT_UNITS_METRIC_MEAN
    idea_id = register_idea(space, _idea_meta(model_id))
    slow = BASELINE_THROUGHPUT / 2  # a real rows/sec figure, far "below" the metric mean
    trial_id = _trial(space, model_id, idea_id, "slow1", throughput=slow, diff_lines=100)
    ledger = _rows(slow1=(1.0, slow, 100))

    with pytest.raises(RegistryValidationError) as excinfo:
        adjudicate_verdict(space, trial_id, ledger)

    assert excinfo.value.field == BASELINE_THROUGHPUT_UNITS_FIELD
    assert "void_throughput_fraction" in str(excinfo.value)  # the refusal names its remedies
    # Nothing was adjudicated: no verdict is recorded against a bar that does not exist.
    assert space.get(trial_id).meta["status"] == "running"
    assert space.get(idea_id).meta.get("status", STATUS_UNTRIED) == STATUS_UNTRIED
    assert space.get(model_id).meta[BASELINE_FIELD] == "r1"


def test_a_metric_mean_model_that_disabled_the_speed_void_adjudicates_normally():
    """The refusal is about a gate that cannot run, not about the model: a campaign that turned
    the speed void off never reaches it and is adjudicated on its metric as usual."""
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), BASELINE_LEDGER)
    space.get(model_id).meta["void_throughput_fraction"] = 0
    idea_id = register_idea(space, _idea_meta(model_id))
    slow = BASELINE_THROUGHPUT / 2
    trial_id = _trial(space, model_id, idea_id, "slow1", throughput=slow, diff_lines=100)

    assert adjudicate_verdict(space, trial_id, _rows(slow1=(1.0, slow, 100))) == VERDICT_PARKED


def test_an_unstamped_model_keeps_the_speed_void():
    """A model registered through plain ``register_model`` -- the path the supervisor's own
    campaigns take -- carries no units stamp and a caller-supplied rows/sec bar. Skipping the
    void for every such model would retire a real guard on all of them, so an ABSENT stamp
    leaves the gate exactly where it was."""
    space = RegistrySpace()
    model_id = register_model(space, dict(
        MODEL_META, noise_floor=NOISE_FLOOR, baseline_throughput=BASELINE_THROUGHPUT,
    ))
    assert BASELINE_THROUGHPUT_UNITS_FIELD not in space.get(model_id).meta
    idea_id = register_idea(space, _idea_meta(model_id))
    slow = BASELINE_THROUGHPUT * 0.94
    trial_id = _trial(space, model_id, idea_id, "slow1", throughput=slow, diff_lines=100)

    assert adjudicate_verdict(space, trial_id, _rows(slow1=(1.0, slow, 100))) == VERDICT_VOIDED


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
    # Each loser would have PARKED against r1, the baseline the adoption displaced, and rejects
    # only against the raised bar -- the one shape that is evidence about the adoption.
    worse_value = ATTRIBUTABLE_LOSS
    for i, (loser_id, commit) in enumerate(zip(loser_ids, ["b1", "b2", "b3"])):
        ledger = _rows(**{commit: (worse_value, BASELINE_THROUGHPUT, 100), "adopt1": (ADOPTED_VALUE, BASELINE_THROUGHPUT, 100)})
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
    # The streak proved the invalidated adoption's baseline was false, so the rejections it
    # produced -- the streak's own included -- were measured against a bar that never
    # existed and go back on the backlog.
    backlog_ids = {i.id for i in untried_backlog(space, model_id=model_id)}
    for loser_id in loser_ids:
        assert loser_id in backlog_ids


def test_deep_losers_that_also_lose_against_the_old_baseline_never_wipe_a_good_adoption():
    """Construction (a), reproduced against the depth threshold this replaced: a GENUINE
    +10-floor adoption followed by three deep exploratory losers -- 20 floors below the new
    baseline, on three distinct axes -- fired the ratchet and reverted a real win.

    Those losers are 10 floors below the PRE-ADOPTION baseline as well: they would have been
    rejected exactly as hard before the adoption existed, so they carry no information about
    whether it was real. The old rule counted them precisely BECAUSE they were deep, which is
    the reading that lets an exploration phase delete the campaign's best result.
    """
    space, model_id = _space_with_model()

    winner_id = register_idea(space, _idea_meta(model_id, "a genuine win", axis="architecture"))
    winner_trial = _trial(space, model_id, winner_id, "adopt1", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    big_win = 1.0 - 10 * NOISE_FLOOR
    assert adjudicate_verdict(
        space, winner_trial, _rows(adopt1=(big_win, BASELINE_THROUGHPUT, 100))
    ) == VERDICT_ADOPTED

    deep_loss = big_win + 20 * NOISE_FLOOR  # == 1.0 + 10 floors: also a rout against r1
    for axis, commit in zip(["data", "architecture", "optimization"], ["deep1", "deep2", "deep3"]):
        loser_id = register_idea(space, _idea_meta(model_id, f"explore-{axis}", axis=axis))
        trial_id = _trial(space, model_id, loser_id, commit, throughput=BASELINE_THROUGHPUT, diff_lines=100)
        ledger = _rows(**{commit: (deep_loss, BASELINE_THROUGHPUT, 100),
                          "adopt1": (big_win, BASELINE_THROUGHPUT, 100)})
        assert adjudicate_verdict(space, trial_id, ledger) == VERDICT_REJECTED
        assert space.get(model_id).meta[REJECTION_STREAK_FIELD] == [], f"{axis} joined the streak"

    model = space.get(model_id)
    assert space.get(winner_id).meta["status"] == STATUS_ADOPTED  # the real win survives
    assert model.meta[BASELINE_FIELD] == "adopt1"
    assert model.meta[PREVIOUS_BASELINE_FIELD] == "r1"
    assert model.meta[RATCHET_COUNT_FIELD] == 0


def test_ratchet_fires_across_three_axes_when_no_axis_ever_repeats():
    """Construction (b), reproduced against the axis-skip this replaced: in a WIDE campaign --
    at most a couple of arms per axis -- the streak axis never repeated, so every rejection
    below the materiality line was skipped and the streak sat pinned at 1 forever while a
    marginally-false adoption stood indefinitely.

    The supervisor makes that worse rather than better: its rabbit-hole watchdog
    (supervisor.py's NON_IMPROVING_STREAK_TRIGGER) EXCLUDES an axis after 2 non-improving
    trials, so it removes the third same-axis rejection the old rule was waiting for.

    Six arms, six ideas, six DIFFERENT axes, each of which would merely have parked against the
    pre-adoption baseline. Attribution does not care which axis found the loss, so the third
    one fires.
    """
    space, model_id = _space_with_model()

    winner_id = register_idea(space, _idea_meta(model_id, "the false adoption", axis="data"))
    winner_trial = _trial(space, model_id, winner_id, "adopt1", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    assert adjudicate_verdict(
        space, winner_trial, _rows(adopt1=(ADOPTED_VALUE, BASELINE_THROUGHPUT, 100))
    ) == VERDICT_ADOPTED

    axes = ["data", "architecture", "optimization", "representation", "augmentation", "loss"]
    fired_after = None
    for i, commit in enumerate(["bad1", "bad2", "bad3", "bad4", "bad5", "bad6"]):
        loser_id = register_idea(space, _idea_meta(model_id, f"wide-loser-{i}", axis=axes[i]))
        trial_id = _trial(space, model_id, loser_id, commit, throughput=BASELINE_THROUGHPUT, diff_lines=100)
        ledger = _rows(**{commit: (ATTRIBUTABLE_LOSS, BASELINE_THROUGHPUT, 100),
                          "adopt1": (ADOPTED_VALUE, BASELINE_THROUGHPUT, 100)})
        assert adjudicate_verdict(space, trial_id, ledger) == VERDICT_REJECTED
        if space.get(winner_id).meta["status"] == STATUS_UNTRIED:
            fired_after = i + 1
            break

    assert fired_after == RATCHET_STREAK_LENGTH, "the ratchet went inert on a campaign that never repeats an axis"
    assert space.get(model_id).meta[BASELINE_FIELD] == "r1"


def test_ratchet_still_fires_on_three_distinct_ideas_on_the_SAME_axis():
    """The axis reset must not defang the ratchet: three distinct ideas all probing the same
    axis are still the same line of attack, and still invalidate the adoption."""
    space, model_id = _space_with_model()

    winner_id = register_idea(space, _idea_meta(model_id, "winner", axis="data"))
    winner_trial = _trial(space, model_id, winner_id, "adopt1", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    assert adjudicate_verdict(
        space, winner_trial, _rows(adopt1=(1.0 - NOISE_FLOOR - 0.01, BASELINE_THROUGHPUT, 100))
    ) == VERDICT_ADOPTED

    worse_value = ATTRIBUTABLE_LOSS
    for i, commit in enumerate(["sameax1", "sameax2", "sameax3"]):
        loser_id = register_idea(space, _idea_meta(model_id, f"same-axis-loser-{i}", axis="data"))
        trial_id = _trial(space, model_id, loser_id, commit, throughput=BASELINE_THROUGHPUT, diff_lines=100)
        ledger = _rows(**{commit: (worse_value, BASELINE_THROUGHPUT, 100),
                          "adopt1": (ADOPTED_VALUE, BASELINE_THROUGHPUT, 100)})
        assert adjudicate_verdict(space, trial_id, ledger) == VERDICT_REJECTED

    model = space.get(model_id)
    assert space.get(winner_id).meta["status"] == STATUS_UNTRIED
    assert model.meta[BASELINE_FIELD] == "r1"
    assert model.meta[RATCHET_COUNT_FIELD] == 0


def test_an_unattributable_rejection_does_not_wipe_the_streak_it_interrupts():
    """A rejection that carries no information about the adoption is SKIPPED, and skipping
    means the streak is left exactly as it was found. An earlier hybrid RESET the streak and
    then appended, so one uninformative arm erased the real evidence in front of it and a
    false adoption survived a mixed run.

    Four rejections: attributable, unattributable, attributable, attributable. The middle one
    neither counts nor costs, so the ratchet fires on the fourth.
    """
    space, model_id = _space_with_model()

    winner_id = register_idea(space, _idea_meta(model_id, "the false adoption", axis="data"))
    winner_trial = _trial(space, model_id, winner_id, "adopt1", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    assert adjudicate_verdict(
        space, winner_trial, _rows(adopt1=(ADOPTED_VALUE, BASELINE_THROUGHPUT, 100))
    ) == VERDICT_ADOPTED

    sequence = [
        ("mix1", "data", ATTRIBUTABLE_LOSS, 1),
        ("mix2", "architecture", UNATTRIBUTABLE_LOSS, 1),
        ("mix3", "optimization", ATTRIBUTABLE_LOSS, 2),
        ("mix4", "data", ATTRIBUTABLE_LOSS, 0),  # fires, and the fire resets the counter
    ]
    for commit, axis, value, expected_count in sequence:
        loser_id = register_idea(space, _idea_meta(model_id, f"mix-{commit}", axis=axis))
        trial_id = _trial(space, model_id, loser_id, commit, throughput=BASELINE_THROUGHPUT, diff_lines=100)
        ledger = _rows(**{commit: (value, BASELINE_THROUGHPUT, 100),
                          "adopt1": (ADOPTED_VALUE, BASELINE_THROUGHPUT, 100)})
        assert adjudicate_verdict(space, trial_id, ledger) == VERDICT_REJECTED
        assert space.get(model_id).meta[RATCHET_COUNT_FIELD] == expected_count, commit

    assert space.get(winner_id).meta["status"] == STATUS_UNTRIED
    assert space.get(model_id).meta[BASELINE_FIELD] == "r1"


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


def test_ratchet_invalidation_requeues_ideas_rejected_under_the_false_baseline():
    """Bug 2 regression: the ratchet fires BECAUSE the adoption is proven to be noise, so
    the baseline it set was false. Every idea rejected during its tenure was judged against
    a bar that never existed and must return to the untried backlog -- otherwise one lucky
    adoption permanently converts good ideas into rejections."""
    space, model_id = _space_with_model()

    winner_id = register_idea(space, _idea_meta(model_id, "the noise adoption"))
    winner_trial = _trial(space, model_id, winner_id, "adopt1", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    adopt_rows = _rows(adopt1=(1.0 - NOISE_FLOOR - 0.01, BASELINE_THROUGHPUT, 100))
    assert adjudicate_verdict(space, winner_trial, adopt_rows) == VERDICT_ADOPTED

    # Rejected while the (about to be discredited) adoption stood.
    casualty_id = register_idea(space, _idea_meta(model_id, "casualty of the false baseline"))
    reject_idea(space, casualty_id, "did not beat the inflated baseline")
    assert casualty_id not in {i.id for i in untried_backlog(space, model_id=model_id)}

    # 3 consecutive worsening rejections on distinct ideas fire the ratchet.
    loser_ids = [register_idea(space, _idea_meta(model_id, f"loser-{i}")) for i in range(3)]
    worse_value = ATTRIBUTABLE_LOSS
    for loser_id, commit in zip(loser_ids, ["b1", "b2", "b3"]):
        ledger = _rows(**{commit: (worse_value, BASELINE_THROUGHPUT, 100),
                          "adopt1": (ADOPTED_VALUE, BASELINE_THROUGHPUT, 100)})
        trial_id = _trial(space, model_id, loser_id, commit, throughput=BASELINE_THROUGHPUT, diff_lines=100)
        assert adjudicate_verdict(space, trial_id, ledger) == VERDICT_REJECTED

    assert space.get(winner_id).meta["status"] == STATUS_UNTRIED
    casualty = space.get(casualty_id)
    assert casualty.meta.get("status", STATUS_UNTRIED) == STATUS_UNTRIED
    assert "rejection_reason" not in casualty.meta
    assert casualty_id in {i.id for i in untried_backlog(space, model_id=model_id)}
    assert casualty_id not in {i.id for i, _ in rejection_memory(space, model_id=model_id)}


def test_a_rejection_that_loses_against_the_previous_baseline_too_is_not_counted():
    """The unit the two adversarial constructions are built out of: the streak counts a
    rejection only when the trial would have PARKED or WON against the bar the adoption
    replaced. This one loses against that bar as well, so it is a worse arm and says nothing
    about the adoption -- rejected, but not evidence."""
    space, model_id = _space_with_model()

    winner_id = register_idea(space, _idea_meta(model_id, "winner"))
    winner_trial = _trial(space, model_id, winner_id, "adopt1", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    assert adjudicate_verdict(
        space, winner_trial, _rows(adopt1=(ADOPTED_VALUE, BASELINE_THROUGHPUT, 100))
    ) == VERDICT_ADOPTED

    loser_id = register_idea(space, _idea_meta(model_id, "a plainly worse arm"))
    trial_id = _trial(space, model_id, loser_id, "wide1", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    ledger = _rows(wide1=(UNATTRIBUTABLE_LOSS, BASELINE_THROUGHPUT, 100),
                   adopt1=(ADOPTED_VALUE, BASELINE_THROUGHPUT, 100))

    assert adjudicate_verdict(space, trial_id, ledger) == VERDICT_REJECTED
    assert space.get(loser_id).meta["status"] == STATUS_REJECTED  # still a rejection
    model = space.get(model_id)
    assert model.meta[REJECTION_STREAK_FIELD] == []
    assert model.meta[RATCHET_COUNT_FIELD] == 0


# ---------------------------------------------------------------------------
# Adversarial: the two holes counterfactual attribution opened, pinned as xfail
# ---------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason="attribution reverts genuine wins: the band it counts IS the win")
def test_a_genuine_win_survives_three_arms_that_merely_reproduce_the_old_baseline():
    """The band attribution counts as evidence is exactly the size of the adoption's win.

    A rejection is ATTRIBUTABLE when it is no worse than one floor below `previous_baseline`,
    and it is a REJECTION when it is more than one floor below the new one -- so the counted
    band runs from (previous - 1 floor) to (new - 1 floor) and is `delta_adopted` floors wide.
    The bigger and more real the win, the wider the window that reverts it. Measured here: a
    +10-floor adoption is erased by three arms that score EXACTLY the pre-adoption baseline.

    An arm that reproduces its parent's number is the single most common outcome in a real
    campaign -- it is what "this idea did nothing" looks like -- so under this rule three
    ordinary no-op arms roll back the campaign's best result, and no baseline that is more
    than two floors better than its predecessor can survive three of them.

    The old depth threshold read this case correctly: an arm level with the OLD baseline is a
    marginal loss against the new one, not the 2-sigma damage that used to be required.
    """
    space, model_id = _space_with_model()

    winner_id = register_idea(space, _idea_meta(model_id, "a real +10 floor win"))
    big_win = 1.0 - 10 * NOISE_FLOOR
    winner_trial = _trial(space, model_id, winner_id, "adopt1", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    assert adjudicate_verdict(
        space, winner_trial, _rows(adopt1=(big_win, BASELINE_THROUGHPUT, 100))
    ) == VERDICT_ADOPTED

    for i, commit in enumerate(["ordinary1", "ordinary2", "ordinary3"]):
        loser_id = register_idea(space, _idea_meta(model_id, f"a no-op arm {i}", axis=f"axis-{i}"))
        trial_id = _trial(space, model_id, loser_id, commit, throughput=BASELINE_THROUGHPUT, diff_lines=100)
        ledger = _rows(**{commit: (1.0, BASELINE_THROUGHPUT, 100),  # exactly the r1 baseline
                          "adopt1": (big_win, BASELINE_THROUGHPUT, 100)})
        assert adjudicate_verdict(space, trial_id, ledger) == VERDICT_REJECTED

    assert space.get(winner_id).meta["status"] == STATUS_ADOPTED
    assert space.get(model_id).meta[BASELINE_FIELD] == "adopt1"


@pytest.mark.xfail(strict=True, reason="attribution goes inert on the bad adoption it exists to roll back")
def test_the_ratchet_still_catches_an_adoption_whose_damage_its_children_inherit():
    """The mirror hole, and the one the supervisor's own integration test still fails on:
    ``test_supervisor.py::test_three_consecutive_dispatch_trial_rejections_on_distinct_ideas_
    fire_the_ratchet_and_invalidate_the_adoption`` dispatches three ``lose*`` arms at 5000.0
    against a baseline of 1.0 and now sees ratchet_count stay 0 for all three.

    Every trial is a commit built ON the current baseline commit, so a genuinely HARMFUL
    adoption is inherited by every arm that follows it and drags each one below the
    PRE-adoption baseline too. Attribution reads exactly that inheritance as "a worse arm,
    says nothing about the adoption" and skips it -- so the harder the adoption damages the
    campaign, the more certainly the ratchet stays silent. Eight consecutive deep rejections
    were measured leaving ratchet_count at 0 and the bad baseline standing.

    Together with the test above, the rule is anti-correlated with the truth it is asking
    about: it fires on the adoptions that were real and goes quiet on the ones that were not.
    """
    space, model_id = _space_with_model()

    bad_id = register_idea(space, _idea_meta(model_id, "a lucky, harmful adoption"))
    bad_trial = _trial(space, model_id, bad_id, "adopt1", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    assert adjudicate_verdict(
        space, bad_trial, _rows(adopt1=(ADOPTED_VALUE, BASELINE_THROUGHPUT, 100))
    ) == VERDICT_ADOPTED

    for i, commit in enumerate(["inherit1", "inherit2", "inherit3"]):
        loser_id = register_idea(space, _idea_meta(model_id, f"child of the bad adoption {i}", axis=f"axis-{i}"))
        trial_id = _trial(space, model_id, loser_id, commit, throughput=BASELINE_THROUGHPUT, diff_lines=100)
        ledger = _rows(**{commit: (UNATTRIBUTABLE_LOSS, BASELINE_THROUGHPUT, 100),
                          "adopt1": (ADOPTED_VALUE, BASELINE_THROUGHPUT, 100)})
        assert adjudicate_verdict(space, trial_id, ledger) == VERDICT_REJECTED

    assert space.get(bad_id).meta["status"] == STATUS_UNTRIED
    assert space.get(model_id).meta[BASELINE_FIELD] == "r1"


def test_a_rejection_counts_when_the_previous_baseline_has_no_ledger_row_to_ask():
    """The counterfactual can be unanswerable -- here the previous baseline commit is not in
    the ledger at all. An unanswerable question leaves the guard where it was rather than
    quietly switching it off, so the rejection counts as it did before."""
    space, model_id = _space_with_model()
    space.get(model_id).meta[PREVIOUS_BASELINE_FIELD] = "not-in-this-ledger"
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "ghost-loser", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    ledger = _rows(**{"ghost-loser": (UNATTRIBUTABLE_LOSS, BASELINE_THROUGHPUT, 100)})

    assert adjudicate_verdict(space, trial_id, ledger) == VERDICT_REJECTED
    assert space.get(model_id).meta[REJECTION_STREAK_FIELD] == [idea_id]


# ---------------------------------------------------------------------------
# Supersession: a better adoption replaces a prior one WITHOUT re-queueing
# ---------------------------------------------------------------------------


def test_a_better_adoption_supersedes_the_prior_one_and_keeps_its_rejections_rejected():
    """Bug 1 regression: supersession used to run the full invalidate_adoption, wiping the
    registry's rejection memory on EVERY successful adoption. The prior adoption was a real
    bar while it stood, so ideas rejected under it were legitimately rejected and stay so."""
    space, model_id = _space_with_model()

    first_id = register_idea(space, _idea_meta(model_id, "first winner"))
    first_value = 1.0 - NOISE_FLOOR - 0.01
    first_trial = _trial(space, model_id, first_id, "win1", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    assert adjudicate_verdict(space, first_trial, _rows(win1=(first_value, BASELINE_THROUGHPUT, 100))) == VERDICT_ADOPTED

    # Rejected while first_id is the active adoption -- stamped with its tenure.
    casualty_id = register_idea(space, _idea_meta(model_id, "legitimately rejected under the first winner"))
    reject_idea(space, casualty_id, "did not beat the first winner")
    assert space.get(casualty_id).meta["rejected_under_adoption"] == first_id

    # A strictly better second winner: it must beat the ADVANCED baseline by > one floor.
    second_id = register_idea(space, _idea_meta(model_id, "second, better winner"))
    second_value = first_value - NOISE_FLOOR - 0.01
    second_trial = _trial(space, model_id, second_id, "win2", throughput=BASELINE_THROUGHPUT, diff_lines=100)
    ledger = _rows(win1=(first_value, BASELINE_THROUGHPUT, 100), win2=(second_value, BASELINE_THROUGHPUT, 100))

    assert adjudicate_verdict(space, second_trial, ledger) == VERDICT_ADOPTED

    # The prior adoption is demoted -- superseded, and emphatically not back on the backlog.
    first = space.get(first_id)
    assert first.meta["status"] == STATUS_SUPERSEDED
    assert "superseded by trial" in first.meta["reversal_reason"]
    assert space.get(second_id).meta["status"] == STATUS_ADOPTED
    assert first_id not in {i.id for i in untried_backlog(space, model_id=model_id)}

    # ...and the rejection memory built under its tenure survives intact.
    casualty = space.get(casualty_id)
    assert casualty.meta["status"] == STATUS_REJECTED
    assert casualty.meta["rejection_reason"] == "did not beat the first winner"
    assert casualty.meta["rejected_under_adoption"] == first_id
    assert casualty_id not in {i.id for i in untried_backlog(space, model_id=model_id)}
    assert dict(
        (i.id, reason) for i, reason in rejection_memory(space, model_id=model_id)
    )[casualty_id] == "did not beat the first winner"


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
