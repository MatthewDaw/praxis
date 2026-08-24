"""R12 acceptance: baseline registration, single-trial adjudication against the rope
recomputed from the ledger, and harness-field retirement with adoption reversal.

R3a retired the threshold a model used to STORE at registration, so the tests that policed
a caller-supplied number went with it -- the recomputation it had to agree with, the
declared method that let it disagree, the magnitude band bounding the disagreement, and
the zero-floor refusal that a deterministic incumbent could never satisfy. What is asserted
here instead is that the rope is measured from the model's own baseline rows at the moment
of each comparison."""

from __future__ import annotations

from collections.abc import Iterable

from pathlib import Path
import statistics

import pytest

from knowledge.ml_registry.floor import (
    BASELINE_FIELD,
    BASELINE_THROUGHPUT_FIELD,
    BASELINE_THROUGHPUT_UNITS_FIELD,
    PREVIOUS_BASELINE_FIELD,
    RATCHET_COUNT_FIELD,
    REJECTION_STREAK_FIELD,
    STALLED,
    THROUGHPUT_UNITS_METRIC_MEAN,
    THROUGHPUT_UNITS_ROWS_PER_SEC,
    adjudicate_trial,
    baseline_values,
    comparison_rope,
    load_ledger_values,
    measure_rope,
    register_model_with_baseline,
    retire_harness,
    revert_adoption,
)
from knowledge.ml_registry.lifecycle import STATUS_ADOPTED, adopt_idea, reject_idea, untried_backlog
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.testing.rope_fixtures import rope_ledger
from knowledge.ml_registry.write_path import RegistrySpace, register_idea, register_trial

MODEL_META: dict[str, object] = {
    "metric": "val_bpb",
    "direction": "minimize",
    "win_condition": "beats baseline by the rope",
    "baseline": "commit-abc123",
    "diff_size_limit": 800,
    "baseline_runs": ["r1", "r2", "r3", "r4"],
}

#: The strict-boundary model's own replicates, measuring a rope of exactly 0.25.
EXACT_COMMITS = ("e1", "e2", "e3", "e4")

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
    # "c-exact" at 0.75 sits exactly one rope (0.25) better than the "e1" baseline.
    **rope_ledger(0.25, at=1.0, commits=EXACT_COMMITS),
    "c-exact": 0.75,
}


def _idea_meta(model_id: str, *, description: str = "try RoPE scaling") -> dict[str, object]:
    return {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": description}


def test_measure_rope_is_sigmas_times_the_sample_stdev_of_the_replicates() -> None:
    assert measure_rope(RUN_VALUES) == pytest.approx(statistics.stdev(RUN_VALUES))
    assert measure_rope(RUN_VALUES, sigmas=2.0) == pytest.approx(2 * statistics.stdev(RUN_VALUES))


@pytest.mark.parametrize("count", [1, 2, 3])
def test_the_rope_refuses_to_be_measured_over_fewer_than_4_runs(count: int) -> None:
    """4 is a MINIMUM, not an exact count -- more repeats are strictly better evidence,
    so only too FEW is a reason to refuse."""
    meta = dict(MODEL_META, baseline_runs=list(MODEL_META["baseline_runs"])[:count])
    with pytest.raises(RegistryValidationError) as excinfo:
        baseline_values(meta, LEDGER)
    assert excinfo.value.field == "baseline_runs"


def test_registration_stores_the_ropes_evidence_and_the_throughput_but_no_threshold() -> None:
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    got = space.get(model_id).meta
    assert got["baseline_runs"] == MODEL_META["baseline_runs"]
    assert got[BASELINE_THROUGHPUT_FIELD] == pytest.approx(statistics.mean(RUN_VALUES))
    assert got[RATCHET_COUNT_FIELD] == 0
    # No bar is stored. The one that decides a verdict is measured here, from those rows.
    assert not [key for key in got if "floor" in key]
    assert comparison_rope(got, baseline_values(got, LEDGER), 1.0) == pytest.approx(
        statistics.stdev(RUN_VALUES)
    )


def test_registration_refuses_a_stored_threshold_against_the_baseline_rows() -> None:
    """The baseline-backed path is where a caller-supplied threshold used to arrive, and the
    recomputation against these rows is what used to police it. The recomputation went with
    the field, so what stands in its place is a refusal: the rope comes from these rows at
    every comparison, and there is no second number left to reconcile with it."""
    space = RegistrySpace()
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model_with_baseline(space, {**MODEL_META, "noise_floor": 999.0}, LEDGER)
    assert excinfo.value.field == "noise_floor"
    assert space.list_facts() == []


def test_registration_refuses_fewer_or_more_than_4_baseline_runs_naming_the_field() -> None:
    space = RegistrySpace()
    meta = dict(MODEL_META)
    meta["baseline_runs"] = ["r1", "r2", "r3"]
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model_with_baseline(space, meta, LEDGER)
    assert excinfo.value.field == "baseline_runs"


def test_registration_refuses_a_baseline_run_commit_missing_from_the_ledger() -> None:
    space = RegistrySpace()
    meta = dict(MODEL_META)
    meta["baseline_runs"] = ["r1", "r2", "r3", "not-in-ledger"]
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model_with_baseline(space, meta, LEDGER)
    assert excinfo.value.field == "baseline_runs"


def test_registration_refuses_a_stored_baseline_throughput_that_disagrees_with_the_recomputation() -> None:
    space = RegistrySpace()
    meta = dict(MODEL_META)
    meta[BASELINE_THROUGHPUT_FIELD] = -1.0
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model_with_baseline(space, meta, LEDGER)
    assert excinfo.value.field == BASELINE_THROUGHPUT_FIELD


def test_registration_accepts_a_stored_throughput_that_agrees_with_the_recomputation() -> None:
    space = RegistrySpace()
    meta = dict(MODEL_META)
    meta[BASELINE_THROUGHPUT_FIELD] = statistics.mean(RUN_VALUES)
    model_id = register_model_with_baseline(space, meta, LEDGER)  # must not raise
    assert space.get(model_id).meta[BASELINE_THROUGHPUT_FIELD] == pytest.approx(
        statistics.mean(RUN_VALUES)
    )


def _trial(space: RegistrySpace, model_id: str, idea_id: str, commit: str,
           ledger: Iterable[str] | None = None) -> str:
    """``ledger`` is anything that iterates the ledger's COMMITS -- the values mapping or a
    bare set of its keys -- since register_trial only checks membership."""
    return register_trial(
        space,
        {"model_id": model_id, "idea_id": idea_id, "commit": commit, "status": "running"},
        frozenset(ledger if ledger is not None else LEDGER),
    )


def test_adjudication_decides_a_candidate_on_a_single_trial_with_no_confirmation_run() -> None:
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


def test_adjudication_reads_the_ledger_value_for_the_trials_own_commit_not_a_reported_one() -> None:
    """The acceptance signal is the EXTERNAL ledger: a trial whose commit scored a losing
    value is failed no matter what number the caller would like it to have been."""
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    idea_id = register_idea(space, _idea_meta(model_id))
    losing_trial = _trial(space, model_id, idea_id, "c-inside")  # ledger says 1.005, a loss

    assert adjudicate_trial(space, losing_trial, LEDGER) == "failed"
    assert space.get(losing_trial).meta["status"] == "failed"
    assert space.get(losing_trial).meta["observed_value"] == pytest.approx(LEDGER["c-inside"])


def test_adjudication_refuses_a_self_reported_value_that_disagrees_with_the_ledger() -> None:
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "c-inside")

    with pytest.raises(RegistryValidationError) as excinfo:
        adjudicate_trial(space, trial_id, LEDGER, self_reported_value=0.5)
    assert excinfo.value.field == "observed_value"
    # refused before anything was written
    assert space.get(trial_id).meta["status"] == "running"


def test_adjudication_accepts_a_self_reported_value_that_agrees_with_the_ledger() -> None:
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "c-win")
    assert adjudicate_trial(space, trial_id, LEDGER, self_reported_value=LEDGER["c-win"]) == "succeeded"


def test_adjudication_refuses_a_commit_with_no_scored_ledger_row_naming_commit() -> None:
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


def test_adjudication_win_test_is_strict_so_it_cannot_disagree_with_adjudicate_verdict() -> None:
    """A delta of EXACTLY one rope is `sigmas` standard deviations, i.e. no evidence:
    verdict.adjudicate_verdict parks it, so floor.adjudicate_trial must not call it a win."""
    space = RegistrySpace()
    # stdev([1.0, 1.0, 1.0, 1.5]) is exactly 0.25, so the boundary is bit-for-bit rather
    # than a hair off it -- and it is MEASURED from these rows, not asserted onto the model.
    meta = dict(MODEL_META, baseline_runs=list(EXACT_COMMITS), baseline="e1")
    model_id = register_model_with_baseline(space, meta, LEDGER)
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "c-exact")  # 0.75 -> delta exactly 0.25

    assert adjudicate_trial(space, trial_id, LEDGER) == "failed"


def test_adjudication_refuses_a_model_whose_baseline_evidence_was_retired() -> None:
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = _trial(space, model_id, idea_id, "c-win")
    retire_harness(space, model_id, {"hardware": "a100"})  # first-time set, not a mutation yet
    retire_harness(space, model_id, {"hardware": "h100"})  # now IT IS a mutation -- retires the evidence

    with pytest.raises(RegistryValidationError) as excinfo:
        adjudicate_trial(space, trial_id, LEDGER)
    assert excinfo.value.field == BASELINE_THROUGHPUT_FIELD


def test_setting_a_harness_field_for_the_first_time_is_not_a_mutation() -> None:
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    before = dict(space.get(model_id).meta)

    retire_harness(space, model_id, {"eval_size": "1000-docs"})

    after = space.get(model_id).meta
    assert after["baseline_runs"] == before["baseline_runs"]
    assert after[RATCHET_COUNT_FIELD] == before[RATCHET_COUNT_FIELD]
    assert after.get("campaign_status") != STALLED
    assert after["eval_size"] == "1000-docs"


def test_a_patch_touching_no_harness_field_is_an_ordinary_update() -> None:
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    retire_harness(space, model_id, {"win_condition": "beats baseline by 2x the rope"})
    after = space.get(model_id).meta
    assert after["win_condition"] == "beats baseline by 2x the rope"
    assert "baseline_runs" in after


def test_mutating_a_recorded_harness_field_retires_the_evidence_clears_the_ratchet_and_stalls() -> None:
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    space.get(model_id).meta[RATCHET_COUNT_FIELD] = 7  # simulate a campaign that had ratcheted forward
    space.get(model_id).meta["hardware"] = "a100"  # already-recorded harness value

    result = retire_harness(space, model_id, {"hardware": "h100"})

    assert BASELINE_THROUGHPUT_FIELD not in result.meta
    assert "baseline_runs" not in result.meta
    assert result.meta[RATCHET_COUNT_FIELD] == 0
    assert result.meta["campaign_status"] == STALLED
    assert result.meta["hardware"] == "h100"


def test_mutating_a_recorded_harness_field_reverts_the_active_adoption_with_its_requeue_side_effects() -> None:
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


def test_mutating_a_harness_field_with_no_active_adoption_does_not_raise() -> None:
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    space.get(model_id).meta["eval_size"] = "1000-docs"
    retire_harness(space, model_id, {"eval_size": "5000-docs"})  # must not raise
    assert space.get(model_id).meta["campaign_status"] == STALLED


def test_retire_harness_refuses_an_unregistered_model_naming_it() -> None:
    space = RegistrySpace()
    with pytest.raises(RegistryValidationError) as excinfo:
        retire_harness(space, "model-does-not-exist", {"hardware": "h100"})
    assert excinfo.value.field == "model_id"


def _model_with_adoption(space: RegistrySpace) -> str:
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


def test_retiring_the_harness_restores_the_baseline_the_reverted_adoption_displaced() -> None:
    """The retired adoption's commit must NOT stay standing as the model's baseline --
    re-registration happens at the baseline left after the reversion, and a dangling
    previous_baseline would leave later trials scored against a repudiated bar."""
    space = RegistrySpace()
    model_id = _model_with_adoption(space)

    retire_harness(space, model_id, {"precision": "fp16"})

    model = space.get(model_id)
    assert model.meta[BASELINE_FIELD] == MODEL_META["baseline"]
    assert PREVIOUS_BASELINE_FIELD not in model.meta


def test_retiring_the_harness_with_no_adoption_leaves_the_baseline_alone() -> None:
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    space.get(model_id).meta["eval_size"] = "1000-docs"

    retire_harness(space, model_id, {"eval_size": "5000-docs"})

    assert space.get(model_id).meta[BASELINE_FIELD] == MODEL_META["baseline"]


def test_revert_adoption_is_the_one_shared_reversion_routine() -> None:
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


def test_revert_adoption_with_nothing_adopted_still_resets_the_ratchet() -> None:
    space = RegistrySpace()
    model_id = register_model_with_baseline(space, dict(MODEL_META), LEDGER)
    space.get(model_id).meta[RATCHET_COUNT_FIELD] = 2

    assert revert_adoption(space, model_id, "ratchet fired with nothing adopted") is False
    assert space.get(model_id).meta[RATCHET_COUNT_FIELD] == 0


def test_revert_adoption_refuses_an_unregistered_model_naming_it() -> None:
    space = RegistrySpace()
    with pytest.raises(RegistryValidationError) as excinfo:
        revert_adoption(space, "model-does-not-exist", "reason")
    assert excinfo.value.field == "model_id"


_LEDGER_HEADER = "commit\tval_bpb\tmemory_gb\tstatus\tdescription\n"


def _write_ledger(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "results.tsv"
    path.write_text(_LEDGER_HEADER + body)
    return path


def test_load_ledger_values_skips_unscored_rows_instead_of_crashing(tmp_path: Path) -> None:
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


def test_a_baseline_run_that_crashed_is_refused_naming_the_offending_commit(tmp_path: Path) -> None:
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


def test_the_rope_honors_sigmas_from_meta() -> None:
    """`sigmas` can no longer contradict the bar -- the registry multiplies by it itself,
    at every comparison, which is the whole of what the court-marking check used to look
    for after the fact."""
    space = RegistrySpace()
    meta = dict(MODEL_META, sigmas=2.0)
    got = space.get(register_model_with_baseline(space, meta, LEDGER)).meta
    assert comparison_rope(got, baseline_values(got, LEDGER), 1.0) == pytest.approx(
        2 * statistics.stdev(RUN_VALUES)
    )


def test_registration_uses_min_throughput_when_throughputs_supplied() -> None:
    space = RegistrySpace()
    throughputs = {"r1": 3.38, "r2": 3.47, "r3": 3.49, "r4": 3.49}
    model_id = register_model_with_baseline(
        space, dict(MODEL_META), LEDGER, ledger_throughputs=throughputs
    )
    assert space.get(model_id).meta[BASELINE_THROUGHPUT_FIELD] == pytest.approx(3.38)
    assert space.get(model_id).meta[BASELINE_THROUGHPUT_FIELD] != pytest.approx(
        statistics.mean(RUN_VALUES)
    )


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


def test_adjudication_compares_the_metric_against_the_metric_baseline_not_a_throughput() -> None:
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


def test_adjudication_refuses_a_rows_per_sec_baseline_it_cannot_replace_with_a_metric() -> None:
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


def test_load_ledger_values_refuses_duplicate_join_keys_naming_them(tmp_path: Path) -> None:
    """Two rows for one key used to leave only the LAST value, silently discarding the
    other run's measurement -- the exact collapse bootstrap's join_keys_unique
    precondition exists to catch, but which nothing re-checked at adjudication time."""
    path = tmp_path / "results.tsv"
    path.write_text("commit\tval_bpb\nabc\t0.70\nabc\t0.95\nother\t1.0\n")
    with pytest.raises(RegistryValidationError) as excinfo:
        load_ledger_values(path)
    assert excinfo.value.field == "commit"
    assert "abc" in str(excinfo.value)


# --- B1: a deterministic incumbent registers; its zero spread is reported, not refused --


def test_a_deterministic_incumbent_registers_and_measures_a_zero_rope() -> None:
    """Four IDENTICAL baseline rows -- what a classical-CV incumbent with no random seed
    produces -- give statistics.stdev exactly 0.0. Registration used to REFUSE that, which
    locked out exactly the campaigns where a baseline is most likely to be deterministic.
    There is no stored number for a zero to corrupt any more, so the measurement is simply
    what it is, and a campaign that needs a positive bar measures one over its scoring
    corpus (policy_gate.compute_campaign_rope) instead of over repeats that cannot vary."""
    space = RegistrySpace()
    deterministic = {"d1": 0.42, "d2": 0.42, "d3": 0.42, "d4": 0.42}
    meta = dict(MODEL_META, baseline_runs=["d1", "d2", "d3", "d4"], baseline="d1")

    got = space.get(register_model_with_baseline(space, meta, deterministic)).meta

    assert got["baseline_runs"] == ["d1", "d2", "d3", "d4"]
    assert comparison_rope(got, baseline_values(got, deterministic), 0.42) == 0.0


# --- B4: more than the minimum baseline runs, and a declared measured floor -----------


def test_registration_accepts_more_than_the_minimum_baseline_runs() -> None:
    """af-seed-ml-supervise's own advice is "if a run is cheap, do more than 4". Logging
    12 baselines used to be refused for having 12 baseline_runs, which left plain
    register-model (which checks nothing) as the only way through."""
    space = RegistrySpace()
    values = {f"b{i}": 1.0 + 0.01 * (i % 5) for i in range(12)}
    meta = dict(MODEL_META, baseline_runs=sorted(values), baseline="b0")
    got = space.get(register_model_with_baseline(space, meta, values)).meta
    # ALL twelve rows are the rope's evidence, not the first four.
    assert comparison_rope(got, baseline_values(got, values), 1.0) == pytest.approx(
        statistics.stdev(values.values())
    )


# --- P4: a scored-but-UNFAIR row and its rerun must not wedge the loader --------------


def test_a_scored_but_unfair_row_and_its_rerun_load_with_the_fair_row_winning(tmp_path: Path) -> None:
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


def test_an_unfair_row_is_still_readable_where_no_fair_row_replaced_it(tmp_path: Path) -> None:
    """Skipping the collision is not deleting the measurement: with no rerun yet, the row
    reads exactly as it did before -- whichever caller needs it decides what it is worth."""
    path = _write_ledger(tmp_path, "sha1:armA\t0.90\t8\tbudget_exhausted\tcut short\n")

    assert load_ledger_values(path) == {"sha1:armA": 0.90}


def test_two_FAIR_rows_under_one_key_are_still_refused(tmp_path: Path) -> None:
    """The unfair-row exemption must not weaken the duplicate detection it sits next to:
    two rows that each claim to be a completed run for one key are the silent
    last-write-wins this refusal exists for."""
    path = _write_ledger(tmp_path, "abc\t0.70\t8\tok\t-\nabc\t0.95\t8\tok\t-\n")

    with pytest.raises(RegistryValidationError) as excinfo:
        load_ledger_values(path)
    assert excinfo.value.field == "commit"
    assert "abc" in str(excinfo.value)


def test_a_ledger_with_no_status_column_still_refuses_duplicates(tmp_path: Path) -> None:
    """A ledger written before the status column existed is older, not broken: every row
    in it reads as fair, so the duplicate refusal is exactly as strict as it always was."""
    path = tmp_path / "results.tsv"
    path.write_text("commit\tval_bpb\nabc\t0.70\nabc\t0.95\n")

    with pytest.raises(RegistryValidationError):
        load_ledger_values(path)


# --- P8: the units stamp is a CLOSED vocabulary, not one string plus silence -----------


def test_registration_refuses_a_units_stamp_it_does_not_recognise() -> None:
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


def test_registration_refuses_a_known_units_stamp_that_contradicts_what_it_computed() -> None:
    """A recognised stamp is still a claim about THIS registration's number: called with no
    ledger_throughputs, baseline_throughput is the metric mean, so a rows_per_sec stamp on
    it would hand every later reader the wrong one of the two meanings."""
    space = RegistrySpace()
    meta = dict(MODEL_META, baseline_throughput_units=THROUGHPUT_UNITS_ROWS_PER_SEC)
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model_with_baseline(space, meta, LEDGER)
    assert excinfo.value.field == BASELINE_THROUGHPUT_UNITS_FIELD


def test_registration_accepts_a_units_stamp_that_agrees_with_what_it_computed() -> None:
    space = RegistrySpace()
    meta = dict(MODEL_META, baseline_throughput_units=THROUGHPUT_UNITS_METRIC_MEAN)
    model_id = register_model_with_baseline(space, meta, LEDGER)  # must not raise
    assert space.get(model_id).meta[BASELINE_THROUGHPUT_UNITS_FIELD] == THROUGHPUT_UNITS_METRIC_MEAN


def test_adjudication_refuses_a_baseline_throughput_stamped_in_units_it_cannot_read() -> None:
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


def test_adjudication_refuses_an_unstamped_baseline_throughput_it_cannot_replace() -> None:
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


def test_an_unstamped_model_still_adjudicates_off_its_scored_baseline_commit() -> None:
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
