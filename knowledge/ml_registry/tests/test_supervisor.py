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
  after that trial's :func:`~knowledge.ml_registry.verdict.adjudicate_verdict` side
  effects have landed, and each non-win close recorded as a completed outcome.
* dispatch_trial routes every trial through verdict.adjudicate_verdict (R10) rather than
  floor.adjudicate_trial, so 3 consecutive worsening-direction rejections on distinct
  ideas fire R10's ratchet and invalidate the active adoption through this module's own
  production entry point, not just when verdict.py is called directly.
"""

from __future__ import annotations

import pytest

from knowledge.ml_registry.floor import CAMPAIGN_STATUS_FIELD, RATCHET_COUNT_FIELD
from knowledge.ml_registry.lifecycle import STATUS_ADOPTED, STATUS_UNTRIED, reject_idea, untried_backlog
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.supervisor import (
    CLOSE_BACKLOG_EXHAUSTED,
    CLOSE_MAX_TRIALS,
    CLOSE_WON,
    Intervention,
    axis_streak,
    dispatch_trial,
    record_keep_pushing_marker,
    resolve_interventions,
    supervise_campaign,
)
from knowledge.ml_registry.verdict import BASELINE_FIELD, LedgerRow
from knowledge.ml_registry.write_path import DISCOVERED, SEEDED, RegistrySpace, register_idea, register_model

BASELINE_COMMIT = "commit-abc123"

MODEL_META = {
    "metric": "val_bpb",
    "direction": "minimize",
    "win_condition": "beats baseline by noise_floor",
    "baseline": BASELINE_COMMIT,
    "noise_floor": 0.01,
    "baseline_throughput": 1200,
    "diff_size_limit": 800,
    "max_trials": 5,
    "max_discovered_ideas": 2,
}

# baseline_throughput=1200, noise_floor=0.01, direction=minimize -> a win needs <= 1199.99,
# every row holds baseline_throughput/diff_lines so a win/reject turns solely on `value`.
# "c*" commits win beyond the noise floor against ANY baseline value used below; "lose*"
# commits worsen far enough (5000.0) to reject against any baseline value used below too.
LEDGER: dict[str, LedgerRow] = {BASELINE_COMMIT: LedgerRow(value=1.0, throughput=1200, diff_lines=0)}
LEDGER.update({f"c{i}": LedgerRow(value=0.5, throughput=1200, diff_lines=100) for i in range(1, 20)})
LEDGER.update({f"lose{i}": LedgerRow(value=5000.0, throughput=1200, diff_lines=100) for i in range(1, 10)})


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
    accumulated across the calls beyond what the registry itself now records. The
    ratchet_count field is owned exclusively by verdict.adjudicate_verdict: an ADOPTED
    verdict resets it to 0 (a fresh baseline earns a fresh streak)."""
    space, model_id = _space_with_model(max_trials=1)
    _idea(space, model_id, "architecture", SEEDED, "winner")

    outcome = supervise_campaign(space, model_id, LEDGER, _scripted_dispatcher(["c1"]))
    assert outcome["close"] == CLOSE_WON
    model = space.get(model_id)
    assert model.meta[RATCHET_COUNT_FIELD] == 0
    assert model.meta[CAMPAIGN_STATUS_FIELD] == CLOSE_WON

    # A second "resume" call against the exhausted backlog recomputes independently.
    outcome2 = supervise_campaign(space, model_id, LEDGER, _scripted_dispatcher([]))
    assert outcome2["close"] == CLOSE_BACKLOG_EXHAUSTED
    assert model.meta[RATCHET_COUNT_FIELD] == 0  # unchanged -- recomputed fresh, still no rejection streak


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


def test_close_evaluated_only_after_adjudication_side_effects_have_landed():
    """A won trial's adjudicate_verdict side effects -- the idea's adoption and the
    model's baseline advancing to the winning commit -- are visible on ``space`` at the
    moment supervise_campaign returns; the close condition is evaluated only AFTER they
    have landed, not before."""
    space, model_id = _space_with_model(max_trials=10)
    winner = _idea(space, model_id, "architecture", SEEDED, "winner")

    outcome = supervise_campaign(space, model_id, LEDGER, _scripted_dispatcher(["c1"]), max_dispatches=1)
    assert outcome["close"] == CLOSE_WON
    assert space.get(winner).meta["status"] == STATUS_ADOPTED
    model = space.get(model_id)
    assert model.meta[BASELINE_FIELD] == "c1"


def test_close_evaluated_only_after_adjudication_side_effects_including_requeue_have_landed():
    """A win that supersedes a prior adoption re-queues whatever was rejected under that
    prior adoption's tenure BEFORE the campaign's close condition is evaluated -- the
    re-queued idea is visible in the backlog at the moment supervise_campaign returns.
    Also asserts, through dispatch_trial (R8's production entry point routed through
    R10's verdict.adjudicate_verdict), that at most one idea per model is ever adopted."""
    space, model_id = _space_with_model(max_trials=10)
    first_winner = _idea(space, model_id, "architecture", SEEDED, "first winner")
    casualty = _idea(space, model_id, "architecture", SEEDED, "rejected under first winner's tenure")
    second_winner = _idea(space, model_id, "architecture", SEEDED, "second winner, supersedes the first")

    # Verdicts compare against the model's CURRENT (advancing) baseline, so the second win
    # needs a commit strictly better than the first win's value (0.5), not merely better
    # than the original baseline (1.0).
    two_wins_ledger = dict(LEDGER)
    two_wins_ledger["c2-better"] = LedgerRow(value=0.2, throughput=1200, diff_lines=100)

    # Trial 1: first_winner succeeds and is adopted.
    r1 = dispatch_trial(space, model_id, two_wins_ledger, _scripted_dispatcher(["c1"]))
    assert r1["candidate"] == first_winner
    assert r1["status"] == "adopted"
    assert space.get(first_winner).meta["status"] == STATUS_ADOPTED

    # casualty gets rejected while first_winner's adoption is active.
    reject_idea(space, casualty, "did not beat baseline")
    assert casualty not in {f.id for f in untried_backlog(space, model_id=model_id)}

    # Trial 2: second_winner also succeeds -- this must invalidate first_winner's adoption
    # and re-queue casualty, and the campaign's close (won) must reflect that landed state.
    outcome = supervise_campaign(
        space, model_id, two_wins_ledger, _scripted_dispatcher(["c2-better"]), max_dispatches=1
    )
    assert outcome["close"] == CLOSE_WON
    assert space.get(first_winner).meta["status"] != STATUS_ADOPTED
    assert space.get(second_winner).meta["status"] == STATUS_ADOPTED
    # re-queued: absence of a status means untried, same as reject_idea's own accounting
    assert space.get(casualty).meta.get("status") in (None, STATUS_UNTRIED)
    assert casualty in {f.id for f in untried_backlog(space, model_id=model_id)}


def test_three_consecutive_dispatch_trial_rejections_on_distinct_ideas_fire_the_ratchet_and_invalidate_the_adoption():
    """Cross-module integration (R8+R10): dispatch_trial routes every trial through
    verdict.adjudicate_verdict, not floor.adjudicate_trial -- so the ratchet feature R10
    built is actually reachable through the campaign supervisor, the production entry
    point (this repros the exact scenario the round #5 post-merge finding used: 3
    consecutive worsening-direction rejections on distinct ideas, beyond noise_floor,
    must set ratchet_count == 3 and, on the 3rd, invalidate the active adoption and
    revert the baseline -- unlike R8's own now-deleted succeeded-trial counter, which for
    this identical sequence would leave ratchet_count stuck at 0 the whole way through)."""
    space, model_id = _space_with_model(max_trials=100)
    winner = _idea(space, model_id, "architecture", SEEDED, "winner")

    win_result = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["c1"]))
    assert win_result["status"] == "adopted"
    model = space.get(model_id)
    assert model.meta[BASELINE_FIELD] == "c1"

    losers = [_idea(space, model_id, "architecture", SEEDED, f"loser-{i}") for i in range(3)]
    ratchet_counts_seen = []
    for expected_loser, commit in zip(losers, ["lose1", "lose2", "lose3"]):
        result = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher([commit]))
        assert result["candidate"] == expected_loser
        assert result["status"] == "rejected"
        ratchet_counts_seen.append(space.get(model_id).meta[RATCHET_COUNT_FIELD])

    # the streak climbed 1, 2, 3 across the first two rejections and the 3rd's own
    # increment, all within adjudicate_verdict's single call for that 3rd trial, and THAT
    # 3rd trial's ratchet_count==3 is what triggers the same call's own invalidation --
    # which then resets the field, so the value PERSISTED after the run is 0 again.
    assert ratchet_counts_seen == [1, 2, 0]
    model = space.get(model_id)
    assert model.meta[RATCHET_COUNT_FIELD] == 0
    assert model.meta[BASELINE_FIELD] == BASELINE_COMMIT  # reverted to the previous baseline
    assert space.get(winner).meta["status"] == STATUS_UNTRIED  # adoption invalidated
    assert "ratchet" in space.get(winner).meta["reversal_reason"]


def test_intervention_rejects_an_unknown_kind():
    with pytest.raises(RegistryValidationError):
        Intervention(kind="bogus", axis="architecture")


# --- R9: axis-watchdog (rabbit-hole + axis-coverage interventions) ---


def test_two_consecutive_non_improving_trials_on_one_axis_auto_excludes_that_axis():
    space, model_id = _space_with_model(max_trials=100)
    _idea(space, model_id, "architecture", SEEDED, "arch-1")
    _idea(space, model_id, "architecture", SEEDED, "arch-2")
    third_arch = _idea(space, model_id, "architecture", SEEDED, "arch-3")
    data_idea = _idea(space, model_id, "data", SEEDED, "data-1")

    r1 = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose1"]))
    assert r1["status"] == "rejected"
    r2 = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose2"]))
    assert r2["status"] == "rejected"

    streak = axis_streak(space, model_id)
    assert streak == {"axis": "architecture", "same_axis_streak": 2, "non_improving_streak": 2}

    # 2 consecutive non-improving trials on "architecture" auto-fire the rabbit-hole
    # intervention -- the next draw skips the untried arch-3 idea and lands on "data".
    r3 = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose3"]))
    assert r3["candidate"] == data_idea
    assert r3["candidate"] != third_arch


def test_keep_pushing_marker_suppresses_the_rabbit_hole_intervention():
    space, model_id = _space_with_model(max_trials=100)
    _idea(space, model_id, "architecture", SEEDED, "arch-1")
    _idea(space, model_id, "architecture", SEEDED, "arch-2")
    third_arch = _idea(space, model_id, "architecture", SEEDED, "arch-3")
    _idea(space, model_id, "data", SEEDED, "data-1")

    dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose1"]))
    dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose2"]))

    record_keep_pushing_marker(space, model_id, "architecture", author="alice")

    r3 = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose3"]))
    assert r3["candidate"] == third_arch  # the marker suppressed the auto-exclude


def test_keep_pushing_marker_must_carry_a_non_empty_author():
    space, model_id = _space_with_model()
    _idea(space, model_id, "architecture", SEEDED, "arch-1")
    with pytest.raises(RegistryValidationError):
        record_keep_pushing_marker(space, model_id, "architecture", author="")


def test_a_value_in_the_dispatcher_payload_never_suppresses_the_rabbit_hole_intervention():
    """A dispatcher trying to suppress the intervention by returning a payload key (rather
    than going through the durable, authored record_keep_pushing_marker call) has no
    effect -- only the durable marker suppresses it."""
    space, model_id = _space_with_model(max_trials=100)
    _idea(space, model_id, "architecture", SEEDED, "arch-1")
    third_arch = _idea(space, model_id, "architecture", SEEDED, "arch-3")
    data_idea = _idea(space, model_id, "data", SEEDED, "data-1")

    def dispatcher_with_suppression_attempt(space, model, idea):
        return {"commit": "lose1", "keep_pushing": True, "keep_pushing_markers": {"architecture": {"author": "eve"}}}

    dispatch_trial(space, model_id, LEDGER, dispatcher_with_suppression_attempt)
    dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose2"]))

    r3 = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose3"]))
    assert r3["candidate"] == data_idea
    assert r3["candidate"] != third_arch


def test_out_of_diff_code_change_resets_the_non_improving_count_exactly_once():
    space, model_id = _space_with_model(max_trials=100)
    _idea(space, model_id, "architecture", SEEDED, "arch-1")
    _idea(space, model_id, "architecture", SEEDED, "arch-2")
    third_arch = _idea(space, model_id, "architecture", SEEDED, "arch-3")
    _idea(space, model_id, "data", SEEDED, "data-1")

    def flagged_dispatcher(commit):
        def dispatcher(space, model, idea):
            return {"commit": commit, "out_of_diff_change": True}
        return dispatcher

    r1 = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose1"]))
    assert r1["status"] == "rejected"
    # trial 2 is flagged as following an out-of-diff code change -- resets the streak to
    # zero before counting its own (non-improving) result, so it lands at 1, not 2.
    r2 = dispatch_trial(space, model_id, LEDGER, flagged_dispatcher("lose2"))
    assert r2["status"] == "rejected"
    assert axis_streak(space, model_id)["non_improving_streak"] == 1

    # so trial 3 still draws from "architecture" -- the reset bought it one more trial.
    r3 = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose3"]))
    assert r3["candidate"] == third_arch


def test_a_second_out_of_diff_reset_on_the_same_axis_run_does_not_reset_again():
    space, model_id = _space_with_model(max_trials=100)
    for i in range(4):
        _idea(space, model_id, "architecture", SEEDED, f"arch-{i}")
    data_idea = _idea(space, model_id, "data", SEEDED, "data-1")

    def flagged_dispatcher(commit):
        def dispatcher(space, model, idea):
            return {"commit": commit, "out_of_diff_change": True}
        return dispatcher

    dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose1"]))          # streak=1
    dispatch_trial(space, model_id, LEDGER, flagged_dispatcher("lose2"))              # reset used -> streak=1
    dispatch_trial(space, model_id, LEDGER, flagged_dispatcher("lose3"))              # 2nd flag ignored -> streak=2

    # the 2nd flag bought no further reset, so the rabbit-hole intervention fires now.
    r4 = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose4"]))
    assert r4["candidate"] == data_idea


def test_five_consecutive_same_axis_trials_force_a_retrieval_axis_pass_even_under_a_marker():
    space, model_id = _space_with_model(max_trials=100)
    for i in range(6):
        _idea(space, model_id, "architecture", SEEDED, f"arch-{i}")
    retrieval_idea = _idea(space, model_id, "current_code", SEEDED, "check current code")

    # a durable marker suppresses the rabbit-hole intervention entirely for "architecture" ...
    record_keep_pushing_marker(space, model_id, "architecture", author="alice")

    commits = [f"lose{i}" for i in range(1, 6)]
    for commit in commits:
        result = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher([commit]))
        assert result["candidate"] != retrieval_idea  # still stuck on "architecture", marker holding

    streak = axis_streak(space, model_id)
    assert streak == {"axis": "architecture", "same_axis_streak": 5, "non_improving_streak": 5}

    # ... but 5 consecutive same-axis trials force a retrieval-axis pass regardless of it.
    r6 = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose6"]))
    assert r6["candidate"] == retrieval_idea
    assert r6["forced_axis"] == "current_code"


def test_axis_streak_is_empty_with_no_trials_and_recomputed_fresh_not_cached():
    space, model_id = _space_with_model()
    assert axis_streak(space, model_id) == {"axis": None, "same_axis_streak": 0, "non_improving_streak": 0}
    _idea(space, model_id, "architecture", SEEDED, "arch-1")
    dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["c1"]))
    # an adopted (improving) trial resets the non-improving streak to zero.
    assert axis_streak(space, model_id) == {"axis": "architecture", "same_axis_streak": 1, "non_improving_streak": 0}
