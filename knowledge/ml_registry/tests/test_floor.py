"""R12 acceptance: noise-floor registration recomputed from the ledger, single-trial
adjudication, and harness-field retirement with adoption reversal."""

from __future__ import annotations

import pytest

from knowledge.ml_registry.floor import (
    BASELINE_THROUGHPUT_FIELD,
    NOISE_FLOOR_FIELD,
    RATCHET_COUNT_FIELD,
    STALLED,
    adjudicate_trial,
    compute_noise_floor,
    register_model_with_baseline,
    retire_harness,
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
LEDGER = {"r1": 1.0, "r2": 1.02, "r3": 0.98, "r4": 1.04, "other": 5.0}


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


def test_adjudication_decides_a_candidate_on_a_single_trial_with_no_confirmation_run():
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = register_trial(space, {"model_id": model_id, "idea_id": idea_id, "commit": "r1", "status": "running"}, frozenset(LEDGER))

    # one call, comfortably past the noise floor -- decided immediately, no repeat call needed
    status = adjudicate_trial(space, trial_id, 0.5)
    assert status == "succeeded"
    assert space.get(trial_id).meta["status"] == "succeeded"

    # a value inside the noise floor margin does not beat baseline -> single-trial loss
    trial_id_2 = register_trial(space, {"model_id": model_id, "idea_id": idea_id, "commit": "r2", "status": "running"}, frozenset(LEDGER))
    status2 = adjudicate_trial(space, trial_id_2, 1.01)
    assert status2 == "failed"


def test_adjudication_refuses_a_model_whose_floor_was_retired():
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = register_trial(space, {"model_id": model_id, "idea_id": idea_id, "commit": "r1", "status": "running"}, frozenset(LEDGER))
    retire_harness(space, model_id, {"hardware": "a100"})  # first-time set, not a mutation yet
    retire_harness(space, model_id, {"hardware": "h100"})  # now it IS a mutation -- retires the floor

    with pytest.raises(RegistryValidationError) as excinfo:
        adjudicate_trial(space, trial_id, 0.5)
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
    trial_id = register_trial(
        space, {"model_id": model_id, "idea_id": winner_id, "commit": "r1", "status": "running"}, frozenset(LEDGER)
    )
    adjudicate_trial(space, trial_id, 0.5)
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
