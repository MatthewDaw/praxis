"""R12 acceptance: noise-floor registration recomputed from the ledger, single-trial
adjudication, and harness-field retirement with adoption reversal."""

from __future__ import annotations

import pytest

from knowledge.ml_registry.floor import (
    BASELINE_FIELD,
    BASELINE_THROUGHPUT_FIELD,
    NOISE_FLOOR_FIELD,
    PREVIOUS_BASELINE_FIELD,
    RATCHET_COUNT_FIELD,
    REJECTION_STREAK_FIELD,
    STALLED,
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
    "win_condition": "beats baseline by noise_floor",
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


@pytest.mark.parametrize("count", [3, 5])
def test_compute_noise_floor_refuses_anything_other_than_4_runs(count):
    with pytest.raises(RegistryValidationError) as excinfo:
        compute_noise_floor(list(RUN_VALUES[:count]) if count <= 4 else RUN_VALUES + [1.0])
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
