"""R12 acceptance: noise-floor registration recomputed from the ledger, single-trial
adjudication, and harness-field retirement with adoption reversal."""

from __future__ import annotations

import statistics

import pytest

from knowledge.ml_registry.floor import (
    BASELINE_FIELD,
    BASELINE_THROUGHPUT_FIELD,
    BASELINE_THROUGHPUT_UNITS_FIELD,
    NOISE_FLOOR_FIELD,
    NOISE_FLOOR_METHOD_FIELD,
    NOISE_FLOOR_METHOD_REPEAT_STDEV,
    NOISE_FLOOR_OVERRIDE_REASON_FIELD,
    PREVIOUS_BASELINE_FIELD,
    RATCHET_COUNT_FIELD,
    REJECTION_STREAK_FIELD,
    STALLED,
    THROUGHPUT_UNITS_METRIC_MEAN,
    THROUGHPUT_UNITS_ROWS_PER_SEC,
    adjudicate_trial,
    compute_noise_floor,
    load_ledger_values,
    register_model_with_baseline,
    retire_harness,
    revert_adoption,
)
from knowledge.ml_registry.lifecycle import STATUS_ADOPTED, adopt_idea, reject_idea, untried_backlog
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.write_path import RegistrySpace, register_idea, register_trial

MODEL_META: dict[str, object] = {
    "metric": "val_bpb",
    "direction": "minimize",
    "win_condition": "beats baseline by the rope",
    "baseline": "commit-abc123",
    "diff_size_limit": 800,
    "baseline_runs": ["r1", "r2", "r3", "r4"],
}

# stdev([1.0, 1.02, 0.98, 1.04], sample) and mean of the same 4 values.
RUN_VALUES = [1.0, 1.02, 0.98, 1.04]
# mean 1.01, sample stdev ~0.02582. "c-win" clears the floor in the minimize direction,
# "c-inside" sits inside it, "c-exact" is exactly one floor better than the mean.
LEDGER = {
    "r1": 1.0,
    "r2": 1.02,
    "r3": 0.98,
    "r4": 1.04,
    "other": 5.0,
    "c-win": 0.5,
    "c-inside": 1.005,
    # exactly-representable pair used by the strict-boundary test, which overrides the
    # model's floor/baseline_throughput to 0.25/1.0 so delta is EXACTLY one floor.
    "c-exact": 0.75,
}


def _idea_meta(model_id, *, description="try RoPE scaling"):
    return {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": description}


def test_compute_noise_floor_is_sample_stdev_and_mean_of_exactly_4_runs():
    import statistics

    floor, throughput = compute_noise_floor(RUN_VALUES)
    assert floor == pytest.approx(statistics.stdev(RUN_VALUES))
    assert throughput == pytest.approx(statistics.mean(RUN_VALUES))


@pytest.mark.parametrize("count", [1, 2, 3])
def test_compute_noise_floor_refuses_fewer_than_4_runs(count):
    """4 is a MINIMUM, not an exact count -- more repeats are strictly better evidence,
    so only too FEW is a reason to refuse."""
    with pytest.raises(RegistryValidationError) as excinfo:
        compute_noise_floor(list(RUN_VALUES[:count]))
    assert excinfo.value.field == "baseline_runs"


def test_registration_recomputes_and_stores_the_floor_and_throughput_from_the_ledger():
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    got = space.get(model_id).meta
    floor, throughput = compute_noise_floor(RUN_VALUES)
    assert got[NOISE_FLOOR_FIELD] == pytest.approx(floor)
    assert got[BASELINE_THROUGHPUT_FIELD] == pytest.approx(throughput)
    assert got[RATCHET_COUNT_FIELD] == 0


def test_registration_refuses_fewer_or_more_than_4_baseline_runs_naming_the_field():
    space = RegistrySpace()
    meta = dict(MODEL_META)
    meta["baseline_runs"] = ["r1", "r2", "r3"]
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model_with_baseline(space, meta, LEDGER)
    assert excinfo.value.field == "baseline_runs"


def test_registration_refuses_a_baseline_run_commit_missing_from_the_ledger():
    space = RegistrySpace()
    meta = dict(MODEL_META)
    meta["baseline_runs"] = ["r1", "r2", "r3", "not-in-ledger"]
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model_with_baseline(space, meta, LEDGER)
    assert excinfo.value.field == "baseline_runs"


def test_registration_refuses_a_stored_noise_floor_that_disagrees_with_the_recomputation():
    space = RegistrySpace()
    meta = dict(MODEL_META)
    meta[NOISE_FLOOR_FIELD] = 999.0
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model_with_baseline(space, meta, LEDGER)
    assert excinfo.value.field == NOISE_FLOOR_FIELD


def test_registration_refuses_a_stored_baseline_throughput_that_disagrees_with_the_recomputation():
    space = RegistrySpace()
    meta = dict(MODEL_META)
    meta[BASELINE_THROUGHPUT_FIELD] = -1.0
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model_with_baseline(space, meta, LEDGER)
    assert excinfo.value.field == BASELINE_THROUGHPUT_FIELD


def test_registration_accepts_a_stored_floor_and_throughput_that_agree_with_the_recomputation():
    space = RegistrySpace()
    floor, throughput = compute_noise_floor(RUN_VALUES)
    meta = dict(MODEL_META)
    meta[NOISE_FLOOR_FIELD] = floor
    meta[BASELINE_THROUGHPUT_FIELD] = throughput
    model_id = register_model_with_baseline(space, meta, LEDGER)  # must not raise
    assert space.get(model_id).meta[NOISE_FLOOR_FIELD] == pytest.approx(floor)


def _trial(space, model_id, idea_id, commit, ledger=None):
    return register_trial(
        space,
        {"model_id": model_id, "idea_id": idea_id, "commit": commit, "status": "running"},
        frozenset(ledger if ledger is not None else LEDGER),
    )


def test_adjudication_decides_a_candidate_on_a_single_trial_with_no_confirmation_run():
    """RETARGETED: this used to pass its own `observed_value` and assert the returned
    status, which certified the self-report path rather than the single-trial property.
    It now supplies only the ledger, and the single-trial property is what is asserted."""
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "c-win")

    # one call, comfortably past the noise floor -- decided immediately, no repeat call needed
    status = adjudicate_trial(space, trial_id, LEDGER)
    assert status == "succeeded"
    assert space.get(trial_id).meta["status"] == "succeeded"

    # a ledger value inside the noise floor margin does not beat baseline -> single-trial loss
    trial_id_2 = _trial(space, model_id, idea_id, "c-inside")
    assert adjudicate_trial(space, trial_id_2, LEDGER) == "failed"


def test_adjudication_reads_the_ledger_value_for_the_trials_own_commit_not_a_reported_one():
    """The acceptance signal is the EXTERNAL ledger: a trial whose commit scored a losing
    value is failed no matter what number the caller would like it to have been."""
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    idea_id = register_idea(space, _idea_meta(model_id))
    losing_trial = _trial(space, model_id, idea_id, "c-inside")  # ledger says 1.005, a loss

    assert adjudicate_trial(space, losing_trial, LEDGER) == "failed"
    assert space.get(losing_trial).meta["status"] == "failed"
    assert space.get(losing_trial).meta["observed_value"] == pytest.approx(LEDGER["c-inside"])


def test_adjudication_refuses_a_self_reported_value_that_disagrees_with_the_ledger():
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "c-inside")

    with pytest.raises(RegistryValidationError) as excinfo:
        adjudicate_trial(space, trial_id, LEDGER, self_reported_value=0.5)
    assert excinfo.value.field == "observed_value"
    # refused before anything was written
    assert space.get(trial_id).meta["status"] == "running"


def test_adjudication_accepts_a_self_reported_value_that_agrees_with_the_ledger():
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "c-win")
    assert adjudicate_trial(space, trial_id, LEDGER, self_reported_value=LEDGER["c-win"]) == "succeeded"


def test_adjudication_refuses_a_commit_with_no_scored_ledger_row_naming_commit():
    """An unscored (crashed) run is an ABSENT measurement, not a loss -- and certainly not
    a win adopted off a number nobody measured."""
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "c-crashed", ledger=set(LEDGER) | {"c-crashed"})

    with pytest.raises(RegistryValidationError) as excinfo:
        adjudicate_trial(space, trial_id, LEDGER)  # ledger has no scored row for c-crashed
    assert excinfo.value.field == "commit"
    assert "c-crashed" in str(excinfo.value)
    assert space.get(trial_id).meta["status"] == "running"


def test_adjudication_win_test_is_strict_so_it_cannot_disagree_with_adjudicate_verdict():
    """A delta of EXACTLY one noise floor is one standard deviation, i.e. no evidence:
    verdict.adjudicate_verdict parks it, so floor.adjudicate_trial must not call it a win."""
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    model = space.get(model_id)
    model.meta[BASELINE_THROUGHPUT_FIELD] = 1.0
    model.meta[NOISE_FLOOR_FIELD] = 0.25
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "c-exact")  # 0.75 -> delta exactly 0.25

    assert adjudicate_trial(space, trial_id, LEDGER) == "failed"


def test_adjudication_refuses_a_model_whose_floor_was_retired():
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "c-win")
    retire_harness(space, model_id, {"hardware": "a100"})  # first-time set, not a mutation yet
    retire_harness(space, model_id, {"hardware": "h100"})  # now it IS a mutation -- retires the floor

    with pytest.raises(RegistryValidationError) as excinfo:
        adjudicate_trial(space, trial_id, LEDGER)
    assert excinfo.value.field == BASELINE_THROUGHPUT_FIELD


def test_setting_a_harness_field_for_the_first_time_is_not_a_mutation():
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    before = dict(space.get(model_id).meta)

    retire_harness(space, model_id, {"eval_size": "1000-docs"})

    after = space.get(model_id).meta
    assert after[NOISE_FLOOR_FIELD] == before[NOISE_FLOOR_FIELD]
    assert after[RATCHET_COUNT_FIELD] == before[RATCHET_COUNT_FIELD]
    assert after.get("campaign_status") != STALLED
    assert after["eval_size"] == "1000-docs"


def test_a_patch_touching_no_harness_field_is_an_ordinary_update():
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    retire_harness(space, model_id, {"win_condition": "beats baseline by 2x noise_floor"})
    after = space.get(model_id).meta
    assert after["win_condition"] == "beats baseline by 2x noise_floor"
    assert NOISE_FLOOR_FIELD in after


def test_mutating_a_recorded_harness_field_retires_the_floor_clears_the_ratchet_and_stalls():
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    space.get(model_id).meta[RATCHET_COUNT_FIELD] = 7  # simulate a campaign that had ratcheted forward
    space.get(model_id).meta["hardware"] = "a100"  # already-recorded harness value

    result = retire_harness(space, model_id, {"hardware": "h100"})

    assert NOISE_FLOOR_FIELD not in result.meta
    assert BASELINE_THROUGHPUT_FIELD not in result.meta
    assert "baseline_runs" not in result.meta
    assert result.meta[RATCHET_COUNT_FIELD] == 0
    assert result.meta["campaign_status"] == STALLED
    assert result.meta["hardware"] == "h100"


def test_mutating_a_recorded_harness_field_reverts_the_active_adoption_with_its_requeue_side_effects():
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    space.get(model_id).meta["precision"] = "fp32"  # already-recorded harness value

    winner_id = register_idea(space, _idea_meta(model_id, description="the eventual winner"))
    loser_id = register_idea(space, _idea_meta(model_id, description="rejected mid-tenure"))
    trial_id = _trial(space, model_id, winner_id, "c-win")
    adjudicate_trial(space, trial_id, LEDGER)
    adopt_idea(space, winner_id, trial_id)
    reject_idea(space, loser_id, "superseded by the winner")  # rejected during winner's tenure

    retire_harness(space, model_id, {"precision": "fp16"})

    winner = space.get(winner_id)
    assert winner.meta["status"] != STATUS_ADOPTED
    assert "harness" in winner.meta["reversal_reason"]
    backlog_ids = {i.id for i in untried_backlog(space)}
    assert loser_id in backlog_ids  # re-queued -- invalidate_adoption's side effect


def test_mutating_a_harness_field_with_no_active_adoption_does_not_raise():
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    space.get(model_id).meta["eval_size"] = "1000-docs"
    retire_harness(space, model_id, {"eval_size": "5000-docs"})  # must not raise
    assert space.get(model_id).meta["campaign_status"] == STALLED


def test_retire_harness_refuses_an_unregistered_model_naming_it():
    space = RegistrySpace()
    with pytest.raises(RegistryValidationError) as excinfo:
        retire_harness(space, "model-does-not-exist", {"hardware": "h100"})
    assert excinfo.value.field == "model_id"


def _model_with_adoption(space):
    """A model whose baseline was advanced by an adoption, as verdict.adjudicate_verdict
    leaves it: baseline == the adopted trial's commit, previous_baseline == what it displaced."""
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    model = space.get(model_id)
    model.meta["precision"] = "fp32"  # already-recorded harness value
    idea_id = register_idea(space, _idea_meta(model_id, description="the adopted winner"))
    trial_id = _trial(space, model_id, idea_id, "c-win")
    adjudicate_trial(space, trial_id, LEDGER)
    adopt_idea(space, idea_id, trial_id)
    model.meta[PREVIOUS_BASELINE_FIELD] = model.meta[BASELINE_FIELD]
    model.meta[BASELINE_FIELD] = "c-win"
    return model_id


def test_retiring_the_harness_restores_the_baseline_the_reverted_adoption_displaced():
    """The retired adoption's commit must NOT stay standing as the model's baseline --
    re-registration happens at the baseline left after the reversion, and a dangling
    previous_baseline would leave later trials scored against a repudiated bar."""
    space = RegistrySpace()
    model_id = _model_with_adoption(space)

    retire_harness(space, model_id, {"precision": "fp16"})

    model = space.get(model_id)
    assert model.meta[BASELINE_FIELD] == MODEL_META["baseline"]
    assert PREVIOUS_BASELINE_FIELD not in model.meta


def test_retiring_the_harness_with_no_adoption_leaves_the_baseline_alone():
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    space.get(model_id).meta["eval_size"] = "1000-docs"

    retire_harness(space, model_id, {"eval_size": "5000-docs"})

    assert space.get(model_id).meta[BASELINE_FIELD] == MODEL_META["baseline"]


def test_revert_adoption_is_the_one_shared_reversion_routine():
    """Both callers (harness retirement here, the ratchet in verdict) must get the same
    three effects: invalidation, baseline restore, ratchet/streak reset."""
    space = RegistrySpace()
    model_id = _model_with_adoption(space)
    model = space.get(model_id)
    model.meta[RATCHET_COUNT_FIELD] = 3
    model.meta[REJECTION_STREAK_FIELD] = ["a", "b", "c"]

    assert revert_adoption(space, model_id, "ratchet fired") is True

    assert model.meta[BASELINE_FIELD] == MODEL_META["baseline"]
    assert model.meta[RATCHET_COUNT_FIELD] == 0
    assert model.meta[REJECTION_STREAK_FIELD] == []
    assert not [i for i in space.list_facts("idea") if i.meta.get("status") == STATUS_ADOPTED]


def test_revert_adoption_with_nothing_adopted_still_resets_the_ratchet():
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    space.get(model_id).meta[RATCHET_COUNT_FIELD] = 2

    assert revert_adoption(space, model_id, "ratchet fired with nothing adopted") is False
    assert space.get(model_id).meta[RATCHET_COUNT_FIELD] == 0


def test_revert_adoption_refuses_an_unregistered_model_naming_it():
    space = RegistrySpace()
    with pytest.raises(RegistryValidationError) as excinfo:
        revert_adoption(space, "model-does-not-exist", "reason")
    assert excinfo.value.field == "model_id"


_LEDGER_HEADER = "commit\tval_bpb\tmemory_gb\tstatus\tdescription\n"


def _write_ledger(tmp_path, body):
    path = tmp_path / "results.tsv"
    path.write_text(_LEDGER_HEADER + body)
    return path


def test_load_ledger_values_skips_unscored_rows_instead_of_crashing(tmp_path):
    """results.tsv carries a status column, so crashed/aborted runs are real rows with an
    empty or non-numeric metric cell -- and some rows are short. One of those must not make
    register-model-with-baseline and supervise-campaign unusable against the real ledger."""
    path = _write_ledger(
        tmp_path,
        "good1\t1.0\t8\tok\tfine\n"
        "crashed\t\t8\tcrashed\tOOM\n"          # empty metric cell
        "aborted\tNaN-ish\t8\taborted\tkilled\n"  # non-numeric metric cell
        "short\n"                                  # short row: no metric column at all
        "good2\t1.02\t8\tok\tfine\n",
    )

    values = load_ledger_values(path)

    assert values == {"good1": 1.0, "good2": 1.02}


def test_a_baseline_run_that_crashed_is_refused_naming_the_offending_commit(tmp_path):
    """Skipping the unscored row does not silently accept it: a caller that actually needs
    that commit is refused, and the commit is named."""
    path = _write_ledger(
        tmp_path,
        "r1\t1.0\t8\tok\t-\nr2\t1.02\t8\tok\t-\nr3\t0.98\t8\tok\t-\nr4\t\t8\tcrashed\tOOM\n",
    )
    space = RegistrySpace()
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model_with_baseline(space, dict(MODEL_META), load_ledger_values(path))
    assert excinfo.value.field == "baseline_runs"
    assert "r4" in str(excinfo.value)


def test_registration_honors_sigmas_from_meta():
    space = RegistrySpace()
    meta = dict(MODEL_META, sigmas=2.0)
    model_id = register_model_with_baseline(space, meta, LEDGER)
    assert space.get(model_id).meta[NOISE_FLOOR_FIELD] == pytest.approx(2 * statistics.stdev(RUN_VALUES))


def test_registration_uses_min_throughput_when_throughputs_supplied():
    space = RegistrySpace()
    throughputs = {"r1": 3.38, "r2": 3.47, "r3": 3.49, "r4": 3.49}
    model_id = register_model_with_baseline(
        space, dict(MODEL_META), LEDGER, ledger_throughputs=throughputs
    )
    assert space.get(model_id).meta[BASELINE_THROUGHPUT_FIELD] == pytest.approx(3.38)
    assert space.get(model_id).meta[BASELINE_THROUGHPUT_FIELD] != pytest.approx(
        statistics.mean(RUN_VALUES)
    )


def test_stored_two_sigma_floor_from_bootstrap_agrees():
    space = RegistrySpace()
    sd = statistics.stdev(RUN_VALUES)
    meta = dict(MODEL_META, sigmas=2.0, noise_floor=round(2 * sd, 6))
    model_id = register_model_with_baseline(space, meta, LEDGER)
    assert space.get(model_id).meta[NOISE_FLOOR_FIELD] == pytest.approx(round(2 * sd, 6))


# --- adjudicate_trial must compare a METRIC against a METRIC baseline ------------------
# `baseline_throughput` carries two incompatible meanings (mean of the baseline metric
# values when registration is given no throughputs, slowest rows/sec when it is), and
# adjudicate_trial read it as the metric bar either way. With bootstrap's own model_meta
# (throughput 3.5 rows/sec) an F1 of 0.99 was adjudicated against 3.5 and "failed".

_TPUT_META: dict[str, object] = {
    "metric": "f1",
    "direction": "maximize",
    "win_condition": "beats baseline by the rope",
    "baseline": "b-metric",
    "diff_size_limit": 800,
    "baseline_runs": ["t1", "t2", "t3", "t4"],
}
_TPUT_VALUES = {"t1": 0.90, "t2": 0.92, "t3": 0.88, "t4": 0.94, "b-metric": 0.90, "c-better": 0.99}
_TPUT_THROUGHPUTS = {c: 3.5 for c in ("t1", "t2", "t3", "t4")}


def test_adjudication_compares_the_metric_against_the_metric_baseline_not_a_throughput():
    """A model registered with real rows/sec throughputs stores 3.5 in baseline_throughput.
    An F1 of 0.99 beats its 0.90 metric baseline by far more than the floor; comparing it
    against 3.5 rows/sec instead made every trial on such a model fail."""
    space = RegistrySpace()
    model_id = register_model_with_baseline(
        space, dict(_TPUT_META), _TPUT_VALUES, ledger_throughputs=_TPUT_THROUGHPUTS
    )
    assert space.get(model_id).meta[BASELINE_THROUGHPUT_FIELD] == pytest.approx(3.5)
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "c-better", ledger=set(_TPUT_VALUES))

    assert adjudicate_trial(space, trial_id, _TPUT_VALUES) == "succeeded"


def test_adjudication_refuses_a_rows_per_sec_baseline_it_cannot_replace_with_a_metric():
    """When baseline_throughput is rows/sec and the model's baseline commit has no scored
    ledger row, there is no metric bar to adjudicate against -- refuse, never fall back to
    comparing the metric with a throughput."""
    space = RegistrySpace()
    model_id = register_model_with_baseline(
        space, dict(_TPUT_META), _TPUT_VALUES, ledger_throughputs=_TPUT_THROUGHPUTS
    )
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "c-better", ledger=set(_TPUT_VALUES))
    without_baseline = {k: v for k, v in _TPUT_VALUES.items() if k != "b-metric"}

    with pytest.raises(RegistryValidationError) as excinfo:
        adjudicate_trial(space, trial_id, without_baseline)
    assert excinfo.value.field == BASELINE_THROUGHPUT_FIELD
    assert space.get(trial_id).meta["status"] == "running"


# --- a duplicate join key must be refused, never silently last-write-win ---------------


def test_load_ledger_values_refuses_duplicate_join_keys_naming_them(tmp_path):
    """Two rows for one key used to leave only the LAST value, silently discarding the
    other run's measurement -- the exact collapse bootstrap's join_keys_unique
    precondition exists to catch, but which nothing re-checked at adjudication time."""
    path = tmp_path / "results.tsv"
    path.write_text("commit\tval_bpb\nabc\t0.70\nabc\t0.95\nother\t1.0\n")
    with pytest.raises(RegistryValidationError) as excinfo:
        load_ledger_values(path)
    assert excinfo.value.field == "commit"
    assert "abc" in str(excinfo.value)


# --- B1: a zero noise floor is refused at registration, never clamped -----------------


def test_registration_refuses_a_zero_floor_from_a_deterministic_incumbent():
    """Four IDENTICAL baseline rows -- what a classical-CV incumbent with no random seed
    produces -- give statistics.stdev exactly 0.0, and a registered floor of 0 is not a
    strict bar but the absence of one: `delta > noise_floor` adopts on a float wobble and
    the symmetric `delta < -noise_floor` rejection leaves the stagnant band a measure-zero
    set, so nothing can ever park. Registration must refuse and name the remedy."""
    space = RegistrySpace()
    deterministic = {"d1": 0.42, "d2": 0.42, "d3": 0.42, "d4": 0.42}
    meta = dict(MODEL_META, baseline_runs=["d1", "d2", "d3", "d4"], baseline="d1")
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model_with_baseline(space, meta, deterministic)
    assert excinfo.value.field == NOISE_FLOOR_FIELD
    message = str(excinfo.value)
    assert "bootstrap" in message and "eval set" in message.lower()
    # Refused, not clamped: nothing was registered.
    assert space.list_facts("model") == []


def test_registration_accepts_a_bootstrap_floor_where_the_repeats_are_degenerate():
    """The escape hatch from the refusal above: a caller who measured the floor by
    bootstrap-resampling the eval set may register it even though the repeats say 0.0."""
    space = RegistrySpace()
    deterministic = {"d1": 0.42, "d2": 0.42, "d3": 0.42, "d4": 0.42}
    meta = dict(
        MODEL_META,
        baseline_runs=["d1", "d2", "d3", "d4"],
        baseline="d1",
        noise_floor=0.011,
        noise_floor_method="bootstrap",
    )
    model_id = register_model_with_baseline(space, meta, deterministic)
    model = space.get(model_id)
    assert model.meta[NOISE_FLOOR_FIELD] == 0.011
    assert model.meta[NOISE_FLOOR_METHOD_FIELD] == "bootstrap"


# --- B4: more than the minimum baseline runs, and a declared measured floor -----------


def test_registration_accepts_more_than_the_minimum_baseline_runs():
    """af-seed-ml-supervise's own advice is "if a run is cheap, do more than 4". Logging
    12 baselines used to be refused for having 12 baseline_runs, which left plain
    register-model (which checks nothing) as the only way through."""
    space = RegistrySpace()
    values = {f"b{i}": 1.0 + 0.01 * (i % 5) for i in range(12)}
    meta = dict(MODEL_META, baseline_runs=sorted(values), baseline="b0")
    model_id = register_model_with_baseline(space, meta, values)
    model = space.get(model_id)
    assert model.meta[NOISE_FLOOR_FIELD] == pytest.approx(statistics.stdev(values.values()))
    assert model.meta[NOISE_FLOOR_METHOD_FIELD] == NOISE_FLOOR_METHOD_REPEAT_STDEV


def test_a_declared_bootstrap_floor_may_disagree_with_the_recomputation():
    space = RegistrySpace()
    meta = dict(MODEL_META, noise_floor=0.004, noise_floor_method="bootstrap")
    model_id = register_model_with_baseline(space, meta, LEDGER)
    model = space.get(model_id)
    assert model.meta[NOISE_FLOOR_FIELD] == 0.004
    assert model.meta[NOISE_FLOOR_METHOD_FIELD] == "bootstrap"


def test_an_undeclared_floor_that_disagrees_is_still_refused():
    """The refusal was never about forcing one method -- it catches a number nobody can
    account for. Without a declared method the recomputation still rules."""
    space = RegistrySpace()
    meta = dict(MODEL_META, noise_floor=0.004)
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model_with_baseline(space, meta, LEDGER)
    assert excinfo.value.field == NOISE_FLOOR_FIELD
    assert NOISE_FLOOR_METHOD_FIELD in str(excinfo.value)


def test_compute_noise_floor_accepts_more_than_the_minimum_and_refuses_fewer():
    assert compute_noise_floor([1.0, 1.02, 0.98, 1.04, 1.01])[0] == pytest.approx(
        statistics.stdev([1.0, 1.02, 0.98, 1.04, 1.01])
    )
    with pytest.raises(RegistryValidationError):
        compute_noise_floor([1.0, 1.02, 0.98])


# --- P4: a scored-but-UNFAIR row and its rerun must not wedge the loader --------------


def test_a_scored_but_unfair_row_and_its_rerun_load_with_the_fair_row_winning(tmp_path):
    """A run cut short but still SCORED -- a numeric metric with status=budget_exhausted,
    the row shape FAIR_RUN_STATUSES exists to describe -- plus the legitimate rerun under
    the same {sha}:{arm_tag} raised the duplicate refusal and made the WHOLE ledger
    unreadable, for every model in it. A voided trial may be re-run (that is what voided
    MEANS), and inventing a new arm tag to get past this corrupts the join key."""
    path = _write_ledger(
        tmp_path,
        "sha1:armA\t0.90\t8\tbudget_exhausted\tcut short at 40% of the eval set\n"
        "sha1:armA\t0.72\t8\tok\tthe rerun that actually finished\n"
        "sha2:armB\t1.0\t8\tok\t-\n",
    )

    assert load_ledger_values(path) == {"sha1:armA": 0.72, "sha2:armB": 1.0}


def test_an_unfair_row_is_still_readable_where_no_fair_row_replaced_it(tmp_path):
    """Skipping the collision is not deleting the measurement: with no rerun yet, the row
    reads exactly as it did before -- whichever caller needs it decides what it is worth."""
    path = _write_ledger(tmp_path, "sha1:armA\t0.90\t8\tbudget_exhausted\tcut short\n")

    assert load_ledger_values(path) == {"sha1:armA": 0.90}


def test_two_FAIR_rows_under_one_key_are_still_refused(tmp_path):
    """The unfair-row exemption must not weaken the duplicate detection it sits next to:
    two rows that each claim to be a completed run for one key are the silent
    last-write-wins this refusal exists for."""
    path = _write_ledger(tmp_path, "abc\t0.70\t8\tok\t-\nabc\t0.95\t8\tok\t-\n")

    with pytest.raises(RegistryValidationError) as excinfo:
        load_ledger_values(path)
    assert excinfo.value.field == "commit"
    assert "abc" in str(excinfo.value)


def test_a_ledger_with_no_status_column_still_refuses_duplicates(tmp_path):
    """A ledger written before the status column existed is older, not broken: every row
    in it reads as fair, so the duplicate refusal is exactly as strict as it always was."""
    path = tmp_path / "results.tsv"
    path.write_text("commit\tval_bpb\nabc\t0.70\nabc\t0.95\n")

    with pytest.raises(RegistryValidationError):
        load_ledger_values(path)


# --- P8: the units stamp is a CLOSED vocabulary, not one string plus silence -----------


def test_registration_refuses_a_units_stamp_it_does_not_recognise():
    """'samples_per_second' is rows/sec by another name, and the campaign that stamped it
    turned the units guard off rather than tripping it: the check knew one literal, so
    every other string read as no opinion at all."""
    space = RegistrySpace()
    meta = dict(MODEL_META, baseline_throughput_units="samples_per_second")
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model_with_baseline(space, meta, LEDGER)
    assert excinfo.value.field == BASELINE_THROUGHPUT_UNITS_FIELD
    assert "samples_per_second" in str(excinfo.value)
    assert space.list_facts("model") == []


def test_registration_refuses_a_known_units_stamp_that_contradicts_what_it_computed():
    """A recognised stamp is still a claim about THIS registration's number: called with no
    ledger_throughputs, baseline_throughput is the metric mean, so a rows_per_sec stamp on
    it would hand every later reader the wrong one of the two meanings."""
    space = RegistrySpace()
    meta = dict(MODEL_META, baseline_throughput_units=THROUGHPUT_UNITS_ROWS_PER_SEC)
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model_with_baseline(space, meta, LEDGER)
    assert excinfo.value.field == BASELINE_THROUGHPUT_UNITS_FIELD


def test_registration_accepts_a_units_stamp_that_agrees_with_what_it_computed():
    space = RegistrySpace()
    meta = dict(MODEL_META, baseline_throughput_units=THROUGHPUT_UNITS_METRIC_MEAN)
    model_id = register_model_with_baseline(space, meta, LEDGER)  # must not raise
    assert space.get(model_id).meta[BASELINE_THROUGHPUT_UNITS_FIELD] == THROUGHPUT_UNITS_METRIC_MEAN


def test_adjudication_refuses_a_baseline_throughput_stamped_in_units_it_cannot_read():
    """An unrecognised stamp must never read as 'no opinion' at adjudication either: that
    is the reading that let a rows/sec number under another name be compared with an F1."""
    space = RegistrySpace()
    model_id = register_model_with_baseline(
        space, dict(_TPUT_META), _TPUT_VALUES, ledger_throughputs=_TPUT_THROUGHPUTS
    )
    model = space.get(model_id)
    model.meta[BASELINE_THROUGHPUT_UNITS_FIELD] = "samples_per_second"
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "c-better", ledger=set(_TPUT_VALUES))
    without_baseline = {k: v for k, v in _TPUT_VALUES.items() if k != "b-metric"}

    with pytest.raises(RegistryValidationError) as excinfo:
        adjudicate_trial(space, trial_id, without_baseline)
    assert excinfo.value.field == BASELINE_THROUGHPUT_UNITS_FIELD
    assert space.get(trial_id).meta["status"] == "running"


# --- P3: an UNSTAMPED baseline_throughput is not evidence that it is a metric mean -----


def test_adjudication_refuses_an_unstamped_baseline_throughput_it_cannot_replace():
    """ledger_throughputs (bae7abb) is OLDER than the stamp (5027002), so a pre-stamp model
    registered through it carries rows/sec and nothing that says so. Reading the ABSENT
    stamp as the legacy metric mean adjudicated an F1 of 0.99 against 3.5 rows/sec and
    called it 'failed' -- the very category error the stamp was added to stop."""
    space = RegistrySpace()
    model_id = register_model_with_baseline(
        space, dict(_TPUT_META), _TPUT_VALUES, ledger_throughputs=_TPUT_THROUGHPUTS
    )
    model = space.get(model_id)
    del model.meta[BASELINE_THROUGHPUT_UNITS_FIELD]  # a model registered before the stamp existed
    assert model.meta[BASELINE_THROUGHPUT_FIELD] == pytest.approx(3.5)
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "c-better", ledger=set(_TPUT_VALUES))
    without_baseline = {k: v for k, v in _TPUT_VALUES.items() if k != "b-metric"}

    with pytest.raises(RegistryValidationError) as excinfo:
        adjudicate_trial(space, trial_id, without_baseline)
    assert excinfo.value.field == BASELINE_THROUGHPUT_UNITS_FIELD
    assert space.get(trial_id).meta["status"] == "running"


def test_an_unstamped_model_still_adjudicates_off_its_scored_baseline_commit():
    """The refusal above is about a bar that cannot be identified, not about the stamp
    being missing: where the baseline commit HAS a scored ledger row, that row is the bar
    and the stamp is never consulted at all."""
    space = RegistrySpace()
    model_id = register_model_with_baseline(
        space, dict(_TPUT_META), _TPUT_VALUES, ledger_throughputs=_TPUT_THROUGHPUTS
    )
    del space.get(model_id).meta[BASELINE_THROUGHPUT_UNITS_FIELD]
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "c-better", ledger=set(_TPUT_VALUES))

    assert adjudicate_trial(space, trial_id, _TPUT_VALUES) == "succeeded"


# --- P6: a DECLARED floor is bounded in magnitude by the rows it was measured beside ---


@pytest.mark.parametrize(
    "floor",
    [1e9, 1e-12],
    ids=["parks every arm forever", "adjudicates float wobble as signal"],
)
def test_a_declared_floor_wildly_out_of_scale_with_its_baseline_rows_is_refused(floor):
    """noise_floor_method admitted ANY string that was not repeat_stdev, and once admitted
    the floor was checked for positivity and nothing else. So the two floors that break a
    campaign in opposite directions both registered cleanly. The declaration is prose; the
    rows are the evidence."""
    space = RegistrySpace()
    meta = dict(MODEL_META, noise_floor=floor, noise_floor_method="bootstrap")
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model_with_baseline(space, meta, LEDGER)
    assert excinfo.value.field == NOISE_FLOOR_FIELD
    assert NOISE_FLOOR_OVERRIDE_REASON_FIELD in str(excinfo.value)
    assert space.list_facts("model") == []


def test_a_floor_outside_the_band_registers_when_the_caller_states_why():
    """The band is a sanity bound, not a ceiling on judgement -- but the way past it is a
    stated reason that stays on the model, so the number remains accountable to whoever
    reads it next."""
    space = RegistrySpace()
    meta = dict(
        MODEL_META,
        noise_floor=1e-6,
        noise_floor_method="bootstrap",
        noise_floor_override_reason="eval set is 400k frames; the repeats' spread is dominated "
        "by a scheduler artefact fixed in this commit",
    )
    model_id = register_model_with_baseline(space, meta, LEDGER)
    assert space.get(model_id).meta[NOISE_FLOOR_FIELD] == 1e-6
    assert space.get(model_id).meta[NOISE_FLOOR_OVERRIDE_REASON_FIELD]


def test_identical_baseline_rows_exempt_a_declared_floor_from_the_band():
    """The exemption is load-bearing, not a loophole: a deterministic incumbent's own stdev
    is exactly 0, which refuses to register as a floor at all, so a bootstrap-resampled
    floor is the ONE way such a model gets one -- the case this whole declared-floor path
    exists for. A zero denominator has no opinion, so it does not get to enforce one."""
    space = RegistrySpace()
    deterministic = {"d1": 0.42, "d2": 0.42, "d3": 0.42, "d4": 0.42}
    meta = dict(
        MODEL_META,
        baseline_runs=["d1", "d2", "d3", "d4"],
        baseline="d1",
        noise_floor=1e9,  # absurd on its face, and still not something the rows can dispute
        noise_floor_method="bootstrap",
    )
    assert register_model_with_baseline(space, meta, deterministic)


# The four registrations this bound must never refuse -- all four are real, all four are
# live. If a future tightening of the band trips any of them, the band is wrong.
_REAL_REGISTRATIONS = [
    ("association", [0.851439] * 10, 0.0016),
    ("detection", [0.6925, 0.6164, 0.6076, 0.6178, 0.5895, 0.5355, 0.5560, 0.6505], 0.099758),
    ("court_marking", [0.155648] * 4, 0.012481),
    ("court_marking_doubled", [0.155648] * 4, 0.024962),
]


@pytest.mark.parametrize("name,values,floor", _REAL_REGISTRATIONS, ids=[r[0] for r in _REAL_REGISTRATIONS])
def test_the_real_campaign_registrations_still_pass_the_band(name, values, floor):
    space = RegistrySpace()
    ledger = {f"{name}-{i}": v for i, v in enumerate(values)}
    runs = sorted(ledger)
    meta = dict(
        MODEL_META, baseline_runs=runs, baseline=runs[0], noise_floor=floor,
        noise_floor_method="bootstrap",
    )
    model_id = register_model_with_baseline(space, meta, ledger)
    assert space.get(model_id).meta[NOISE_FLOOR_FIELD] == floor


def test_contact_points_floor_measured_over_MORE_rows_than_baseline_runs_names_passes():
    """contact_point's floor is 2 sigma of all 12 baseline rows, but baseline_runs names
    only 4 of them, whose own 2 sigma is 0.005553 -- so the stored floor is 1.28x the named
    rows' two-sigma and 2.56x their stdev. A bound that only accepted the named rows'
    exact recomputation would refuse a floor measured off BETTER evidence."""
    space = RegistrySpace()
    step = (0.005553 / 2) / statistics.stdev([-1.5, -0.5, 0.5, 1.5])
    named = {f"cp{i}": 0.10 + k * step for i, k in enumerate((-1.5, -0.5, 0.5, 1.5))}
    assert 2 * statistics.stdev(named.values()) == pytest.approx(0.005553)
    ledger = dict(named, **{f"cp-other{i}": v for i, v in enumerate([0.09, 0.11, 0.12, 0.08])})
    meta = dict(
        MODEL_META, baseline_runs=sorted(named), baseline="cp0", noise_floor=0.007104,
        noise_floor_method="bootstrap",
    )
    model_id = register_model_with_baseline(space, meta, ledger)
    assert space.get(model_id).meta[NOISE_FLOOR_FIELD] == 0.007104
