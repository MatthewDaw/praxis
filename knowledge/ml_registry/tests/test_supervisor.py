"""R8 acceptance: the campaign supervisor drives one model's autoresearch loop to close,
dispatching one worker per trial serially.

Builds on R2's write path, R3/R4's idea lifecycle + query surface, and R11's campaign
budgets (:mod:`knowledge.ml_registry.write_path`, :mod:`knowledge.ml_registry.lifecycle`).
Covers, directly against :mod:`knowledge.ml_registry.supervisor`:

* dispatching three trials produces three distinct worker sessions run one at a time.
* seed-first draw order within permitted axes.
* a forced-axis intervention taking precedence over seed-first.
* an exclude-axis intervention that would leave nothing untried outside it being recorded
  unsatisfiable, and NOT applied, before seed-first proceeds.
* a discovered idea registered with an axis, a basis and origin=discovered BEFORE its
  trial is recorded.
* every intervention/ratchet counter recomputed from the registry on each call, never
  carried in caller-held state.
* a voided trial not counting against max_trials.
* the three close conditions (win, backlog exhausted, max_trials), each evaluated only
  after that trial's adjudication side effects (including any re-queue) have landed, and
  each non-win close recorded as a completed outcome.
"""

from __future__ import annotations

import pytest

from knowledge.ml_registry.floor import CAMPAIGN_STATUS_FIELD, RATCHET_COUNT_FIELD
from knowledge.ml_registry.lifecycle import STATUS_ADOPTED, STATUS_UNTRIED, untried_backlog
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.supervisor import (
    CLOSE_BACKLOG_EXHAUSTED,
    CLOSE_MAX_TRIALS,
    CLOSE_WON,
    Intervention,
    dispatch_trial,
    resolve_interventions,
    supervise_campaign,
)
from knowledge.ml_registry.write_path import DISCOVERED, SEEDED, RegistrySpace, register_idea, register_model

MODEL_META = {
    "metric": "val_bpb",
    "direction": "minimize",
    "win_condition": "beats baseline by noise_floor",
    "baseline": "commit-abc123",
    "noise_floor": 0.01,
    "baseline_throughput": 1200,
    "diff_size_limit": 800,
    "max_trials": 5,
    "max_discovered_ideas": 2,
}

# baseline_throughput=1200, noise_floor=0.01, direction=minimize -> a win needs <= 1199.99.
LEDGER = {f"c{i}": 100.0 for i in range(1, 20)}  # every "c*" commit wins
LEDGER.update({f"lose{i}": 5000.0 for i in range(1, 10)})  # every "lose*" commit fails


def _space_with_model(**overrides) -> tuple[RegistrySpace, str]:
    space = RegistrySpace()
    model_id = register_model(space, {**MODEL_META, **overrides})
    return space, model_id


def _idea(space, model_id, axis, origin, description="idea"):
    return register_idea(
        space, {"model_id": model_id, "origin": origin, "axis": axis, "description": description}
    )


def _scripted_dispatcher(commits: list[str]):
    """A dispatcher that hands out ``commits`` in order, one per call -- the CLI's own
    dispatch-script mechanism, in-process."""
    it = iter(commits)

    def dispatcher(space, model, idea):
        return {"commit": next(it)}

    return dispatcher


def test_dispatching_three_trials_produces_three_distinct_worker_sessions_run_one_at_a_time():
    space, model_id = _space_with_model(max_trials=100)
    for i in range(3):
        _idea(space, model_id, "architecture", SEEDED, f"seed-{i}")
    dispatcher = _scripted_dispatcher(["lose1", "lose2", "lose3"])

    seen_calls = []

    def counting_dispatcher(s, model, idea):
        seen_calls.append(idea.id)
        return dispatcher(s, model, idea)

    outcome = supervise_campaign(space, model_id, LEDGER, counting_dispatcher, max_dispatches=3)
    assert len(outcome["history"]) == 3
    assert len(seen_calls) == 3
    assert len(set(seen_calls)) == 3  # three DISTINCT ideas -- one worker session per trial
    trial_ids = [h["trial_id"] for h in outcome["history"]]
    assert len(set(trial_ids)) == 3  # three distinct trials


def test_seed_first_draw_order_prefers_untried_seeded_idea_on_a_permitted_axis():
    space, model_id = _space_with_model()
    _idea(space, model_id, "architecture", DISCOVERED, "discovered-first-registered")
    seeded_id = _idea(space, model_id, "architecture", SEEDED, "seeded-second-registered")

    dispatcher = _scripted_dispatcher(["lose1"])
    result = dispatch_trial(space, model_id, LEDGER, dispatcher)
    assert result["candidate"] == seeded_id
    assert result["origin"] == SEEDED


def test_forced_axis_intervention_takes_precedence_over_seed_first():
    space, model_id = _space_with_model()
    _idea(space, model_id, "architecture", SEEDED, "seeded on architecture")
    seeded_forced_axis = _idea(space, model_id, "data", SEEDED, "seeded on data")

    dispatcher = _scripted_dispatcher(["lose1"])
    result = dispatch_trial(
        space, model_id, LEDGER, dispatcher, interventions=(Intervention(kind="forced_axis", axis="data"),)
    )
    assert result["candidate"] == seeded_forced_axis
    assert result["forced_axis"] == "data"


def test_exclude_axis_intervention_leaving_nothing_untried_elsewhere_is_recorded_unsatisfiable():
    space, model_id = _space_with_model()
    only_idea = _idea(space, model_id, "architecture", SEEDED, "only untried idea, on the excluded axis")

    iv = Intervention(kind="exclude_axis", axis="architecture")
    forced_axis, permitted_axes, unsatisfiable = resolve_interventions(space, model_id, (iv,))
    assert unsatisfiable == [iv]
    assert "architecture" in permitted_axes  # the exclusion was NOT applied

    # seed-first proceeds despite the (unsatisfiable) exclusion -- the only idea still gets picked.
    dispatcher = _scripted_dispatcher(["lose1"])
    result = dispatch_trial(space, model_id, LEDGER, dispatcher, interventions=(iv,))
    assert result["candidate"] == only_idea
    assert result["unsatisfiable_interventions"] == [iv]


def test_exclude_axis_intervention_applies_when_something_remains_outside_it():
    space, model_id = _space_with_model()
    _idea(space, model_id, "architecture", SEEDED, "on excluded axis")
    permitted_axis_idea = _idea(space, model_id, "data", SEEDED, "on permitted axis")

    iv = Intervention(kind="exclude_axis", axis="architecture")
    dispatcher = _scripted_dispatcher(["lose1"])
    result = dispatch_trial(space, model_id, LEDGER, dispatcher, interventions=(iv,))
    assert result["candidate"] == permitted_axis_idea
    assert result["unsatisfiable_interventions"] == []


def test_discovered_idea_is_registered_with_axis_basis_and_origin_before_its_trial():
    space, model_id = _space_with_model()  # backlog is empty

    def idea_generator(space, model_id, forced_axis, permitted_axes):
        return {"axis": "optimizer", "description": "try a new LR schedule", "basis": "reasoned"}

    dispatcher = _scripted_dispatcher(["lose1"])
    result = dispatch_trial(space, model_id, LEDGER, dispatcher, idea_generator=idea_generator)

    registered = space.get(result["candidate"])
    assert registered is not None
    assert registered.meta["origin"] == DISCOVERED
    assert registered.meta["axis"] == "optimizer"
    assert registered.meta["basis"] == "reasoned"
    # the trial that came out of this dispatch really does derive_from the discovered idea
    trial = space.get(result["trial_id"])
    assert trial.derived_from == (registered.id,)


def test_ratchet_counter_and_close_condition_are_recomputed_not_carried_in_state():
    """Two campaign() calls against the SAME registry state (simulating a resumed
    process) recompute the identical ratchet_count and close outcome -- nothing is
    accumulated across the calls beyond what the registry itself now records."""
    space, model_id = _space_with_model(max_trials=1)
    _idea(space, model_id, "architecture", SEEDED, "winner")

    outcome = supervise_campaign(space, model_id, LEDGER, _scripted_dispatcher(["c1"]))
    assert outcome["close"] == CLOSE_WON
    model = space.get(model_id)
    assert model.meta[RATCHET_COUNT_FIELD] == 1
    assert model.meta[CAMPAIGN_STATUS_FIELD] == CLOSE_WON

    # A second "resume" call against the exhausted backlog recomputes independently.
    outcome2 = supervise_campaign(space, model_id, LEDGER, _scripted_dispatcher([]))
    assert outcome2["close"] == CLOSE_BACKLOG_EXHAUSTED
    assert model.meta[RATCHET_COUNT_FIELD] == 1  # unchanged -- recomputed fresh, still 1 succeeded trial


def test_voided_trial_does_not_count_against_max_trials():
    """The first dispatch comes back voided (e.g. infra failure); the idea it drew stays
    untried, so the SAME idea is redrawn on the next dispatch -- this time it actually
    runs. With max_trials=1, only that second (non-voided, failing) trial should close
    the campaign; the voided one must not have counted toward the budget."""
    space, model_id = _space_with_model(max_trials=1)
    _idea(space, model_id, "architecture", SEEDED, "the only untried idea")

    calls = {"n": 0}

    def dispatcher(space, model, idea):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"commit": "c1", "status": "voided"}
        return {"commit": "lose1"}

    outcome = supervise_campaign(space, model_id, LEDGER, dispatcher, max_dispatches=2)
    assert outcome["history"][0]["status"] == "voided"
    # the voided trial did not close the campaign on max_trials=1 -- the second (real,
    # failing) trial is the one that hits the max_trials=1 budget.
    assert outcome["close"] == CLOSE_MAX_TRIALS
    assert len(outcome["history"]) == 2


def test_close_on_max_trials_is_recorded_as_a_completed_outcome():
    space, model_id = _space_with_model(max_trials=1)
    _idea(space, model_id, "architecture", SEEDED, "loser")
    outcome = supervise_campaign(space, model_id, LEDGER, _scripted_dispatcher(["lose1"]))
    assert outcome["close"] == CLOSE_MAX_TRIALS
    model = space.get(model_id)
    assert model.meta[CAMPAIGN_STATUS_FIELD] == "completed"


def test_close_on_backlog_exhausted_is_recorded_as_a_completed_outcome():
    space, model_id = _space_with_model()  # no ideas at all, no idea_generator
    outcome = supervise_campaign(space, model_id, LEDGER, _scripted_dispatcher([]))
    assert outcome["close"] == CLOSE_BACKLOG_EXHAUSTED
    assert outcome["history"] == [
        {"unsatisfiable_interventions": [], "forced_axis": None, "candidate": None}
    ]
    model = space.get(model_id)
    assert model.meta[CAMPAIGN_STATUS_FIELD] == "completed"


def test_close_evaluated_only_after_adjudication_side_effects_including_requeue_have_landed():
    """A win that supersedes a prior adoption re-queues whatever was rejected under that
    prior adoption's tenure BEFORE the campaign's close condition is evaluated -- the
    re-queued idea is visible in the backlog at the moment supervise_campaign returns."""
    space, model_id = _space_with_model(max_trials=10)
    first_winner = _idea(space, model_id, "architecture", SEEDED, "first winner")
    casualty = _idea(space, model_id, "architecture", SEEDED, "rejected under first winner's tenure")
    second_winner = _idea(space, model_id, "architecture", SEEDED, "second winner, supersedes the first")

    from knowledge.ml_registry.lifecycle import reject_idea

    # Trial 1: first_winner succeeds and is adopted.
    r1 = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["c1"]))
    assert r1["candidate"] == first_winner
    assert r1["status"] == "succeeded"

    # casualty gets rejected while first_winner's adoption is active.
    reject_idea(space, casualty, "did not beat baseline")
    assert casualty not in {f.id for f in untried_backlog(space, model_id=model_id)}

    # Trial 2: second_winner also succeeds -- this must invalidate first_winner's adoption
    # and re-queue casualty, and the campaign's close (won) must reflect that landed state.
    outcome = supervise_campaign(space, model_id, LEDGER, _scripted_dispatcher(["c2"]), max_dispatches=1)
    assert outcome["close"] == CLOSE_WON
    assert space.get(first_winner).meta["status"] != STATUS_ADOPTED
    assert space.get(second_winner).meta["status"] == STATUS_ADOPTED
    # re-queued: absence of a status means untried, same as reject_idea's own accounting
    assert space.get(casualty).meta.get("status") in (None, STATUS_UNTRIED)
    assert casualty in {f.id for f in untried_backlog(space, model_id=model_id)}


def test_intervention_rejects_an_unknown_kind():
    with pytest.raises(RegistryValidationError):
        Intervention(kind="bogus", axis="architecture")
