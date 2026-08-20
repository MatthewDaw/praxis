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
from knowledge.ml_registry.lifecycle import (
    STATUS_ADOPTED,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
    STATUS_UNTRIED,
    reject_idea,
    untried_backlog,
)
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.supervisor import (
    CLOSE_BACKLOG_EXHAUSTED,
    CLOSE_MAX_TRIALS,
    CLOSE_TRIAL_TIMEOUT,
    CLOSE_VOID_LIMIT,
    CLOSE_WON,
    DEFAULT_MAX_CONSECUTIVE_VOIDS,
    TRIAL_STATUS_TIMED_OUT,
    Intervention,
    axis_streak,
    check_win_on_adoption_declared,
    consecutive_void_count,
    dispatch_trial,
    parse_win_condition,
    NON_IMPROVING_STREAK_TRIGGER,
    model_non_improving_trigger,
    record_keep_pushing_marker,
    record_out_of_diff_change,
    resolve_interventions,
    supervise_campaign,
)
from knowledge.ml_registry.verdict import BASELINE_FIELD, LedgerRow
from knowledge.ml_registry.write_path import DISCOVERED, SEEDED, RegistrySpace, register_idea, register_model

BASELINE_COMMIT = "commit-abc123"

MODEL_META = {
    "metric": "val_bpb",
    "direction": "minimize",
    # These fixtures genuinely mean first-adoption-wins -- most of them dispatch one or two
    # scripted trials and assert on the close -- so they DECLARE it, which is exactly the
    # declaration check_win_on_adoption_declared asks a real campaign for.
    "win_condition": "beats baseline by noise_floor",
    "win_on_adoption_ok": True,
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
# "near*" commits reject against an ADOPTED baseline but would have parked against the 1.0 the
# adoption replaced. That gap is the whole question the ratchet asks: a loss only counts as
# evidence the adoption was noise if the raised bar is what caused it. A "lose*" arm at 5000.0
# loses against both bars, says nothing about the adoption, and deliberately does not count.
LEDGER.update({f"near{i}": LedgerRow(value=1.0, throughput=1200, diff_lines=100) for i in range(1, 10)})
# "slow*" commits are the ONLY way a trial gets voided: their LEDGER throughput falls more
# than THROUGHPUT_FLOOR_FRACTION (5%) below baseline_throughput=1200, so adjudicate_verdict
# -- and only adjudicate_verdict, never a dispatcher's self-report -- voids them.
LEDGER.update({f"slow{i}": LedgerRow(value=0.5, throughput=1000, diff_lines=100) for i in range(1, 10)})


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


def test_ledger_voided_trial_does_not_count_against_max_trials():
    """RETARGETED (was: the dispatcher SELF-REPORTED ``status="voided"`` and this test
    blessed it). A void is an adjudication against the EXTERNAL LEDGER, never a self-report:
    the first dispatch's commit throughput falls below the model's baseline_throughput floor
    in the ledger, so adjudicate_verdict voids it. The idea it drew stays untried and is
    redrawn on the next dispatch -- this time on a commit the ledger scores normally. With
    max_trials=1, only that second (non-voided, failing) trial closes the campaign."""
    space, model_id = _space_with_model(max_trials=1)
    _idea(space, model_id, "architecture", SEEDED, "the only untried idea")

    calls = {"n": 0}

    def dispatcher(space, model, idea):
        calls["n"] += 1
        return {"commit": "slow1"} if calls["n"] == 1 else {"commit": "lose1"}

    outcome = supervise_campaign(space, model_id, LEDGER, dispatcher, max_dispatches=2)
    assert outcome["history"][0]["status"] == "voided"
    # the voided trial did not close the campaign on max_trials=1 -- the second (real,
    # failing) trial is the one that hits the max_trials=1 budget.
    assert outcome["close"] == CLOSE_MAX_TRIALS
    assert len(outcome["history"]) == 2


def test_a_dispatcher_cannot_self_report_its_own_trial_voided():
    """The judged party does not get to declare its own verdict: a dispatcher returning
    ``status="voided"`` on a commit the LEDGER scores perfectly well is adjudicated exactly
    as if it had said nothing -- the trial is registered, adjudicated, and (here) adopted."""
    space, model_id = _space_with_model(max_trials=10)
    winner = _idea(space, model_id, "architecture", SEEDED, "winner claiming to be voided")

    def lying_dispatcher(space, model, idea):
        return {"commit": "c1", "status": "voided"}

    result = dispatch_trial(space, model_id, LEDGER, lying_dispatcher)
    assert result["status"] == "adopted"
    assert space.get(result["trial_id"]).meta["status"] != "voided"
    assert space.get(winner).meta["status"] == STATUS_ADOPTED


def test_a_self_reported_void_can_no_longer_make_the_campaign_non_terminating():
    """A dispatcher that reports itself voided on every call used to leave its idea untried
    forever, excluded from max_trials, redrawn for ever -- an unbounded campaign. Now its
    self-report is discarded and the trials adjudicate normally, so the campaign closes."""
    space, model_id = _space_with_model(max_trials=2)
    _idea(space, model_id, "architecture", SEEDED, "arch-1")
    _idea(space, model_id, "architecture", SEEDED, "arch-2")

    def always_claims_voided(space, model, idea):
        return {"commit": "lose1" if idea.meta["description"] == "arch-1" else "lose2", "status": "voided"}

    outcome = supervise_campaign(space, model_id, LEDGER, always_claims_voided)  # no max_dispatches
    assert outcome["close"] == CLOSE_MAX_TRIALS


def test_consecutive_ledger_voids_are_bounded_and_close_the_campaign():
    """A harness that keeps producing unreliable (ledger-voided) runs cannot be redrawn
    forever: DEFAULT_MAX_CONSECUTIVE_VOIDS voids in a row close the campaign, and the count
    is recomputed from the registry's trial history rather than held in loop state."""
    space, model_id = _space_with_model(max_trials=100)
    _idea(space, model_id, "architecture", SEEDED, "the only untried idea")

    calls = {"n": 0}

    def always_slow(space, model, idea):
        calls["n"] += 1
        return {"commit": f"slow{calls['n']}"}

    outcome = supervise_campaign(space, model_id, LEDGER, always_slow)  # no max_dispatches
    assert outcome["close"] == CLOSE_VOID_LIMIT
    assert len(outcome["history"]) == DEFAULT_MAX_CONSECUTIVE_VOIDS
    assert consecutive_void_count(space, model_id) == DEFAULT_MAX_CONSECUTIVE_VOIDS
    assert space.get(model_id).meta[CAMPAIGN_STATUS_FIELD] == "completed"


def test_a_voided_trial_never_reaches_the_cross_model_lesson_gate(monkeypatch):
    """dispatch_trial's docstring promises a voided trial never reaches the R17 lesson-filing
    gate (its idea reached no terminal status). Assert the gate is not even called."""
    import knowledge.ml_registry.supervisor as supervisor_module

    calls = []
    monkeypatch.setattr(
        supervisor_module, "maybe_file_cross_model_lesson",
        lambda *a, **kw: calls.append(a) or None,
    )

    space, model_id = _space_with_model(max_trials=10)
    _idea(space, model_id, "architecture", SEEDED, "voided")
    voided = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["slow1"]))
    assert voided["status"] == "voided"
    assert calls == []

    # ... whereas an adjudicated (terminal) trial does reach it.
    dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose1"]))
    assert len(calls) == 1


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


def test_close_evaluated_only_after_a_supersession_has_landed():
    """A win that supersedes a prior adoption demotes that prior adoption BEFORE the
    campaign's close condition is evaluated -- the demotion is visible at the moment
    supervise_campaign returns. Also asserts, through dispatch_trial (R8's production entry
    point routed through R10's verdict.adjudicate_verdict), that at most one idea per model
    is ever adopted, and that supersession does NOT re-queue the prior tenure's rejections:
    the prior adoption was a real bar while it stood, so those rejections remain valid."""
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

    # Trial 2: second_winner also succeeds -- this must supersede first_winner's adoption,
    # and the campaign's close (won) must reflect that landed state.
    outcome = supervise_campaign(
        space, model_id, two_wins_ledger, _scripted_dispatcher(["c2-better"]), max_dispatches=1
    )
    assert outcome["close"] == CLOSE_WON
    assert space.get(first_winner).meta["status"] == STATUS_SUPERSEDED
    assert space.get(second_winner).meta["status"] == STATUS_ADOPTED
    # Superseded, not invalidated: casualty was rejected against a bar that really stood,
    # so it stays rejected and off the backlog.
    assert space.get(casualty).meta["status"] == STATUS_REJECTED
    assert space.get(casualty).meta["rejection_reason"] == "did not beat baseline"
    assert casualty not in {f.id for f in untried_backlog(space, model_id=model_id)}
    assert first_winner not in {f.id for f in untried_backlog(space, model_id=model_id)}


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
    for expected_loser, commit in zip(losers, ["near1", "near2", "near3"]):
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


def test_an_authored_out_of_diff_change_resets_the_non_improving_count_exactly_once():
    """RETARGETED (was: the reset was claimed by the judged dispatcher's own trial payload).
    An out-of-diff change is authored OUT OF BAND -- record_out_of_diff_change, exactly like
    the keep-pushing marker -- and only the first one within a same-axis run resets."""
    space, model_id = _space_with_model(max_trials=100)
    _idea(space, model_id, "architecture", SEEDED, "arch-1")
    _idea(space, model_id, "architecture", SEEDED, "arch-2")
    third_arch = _idea(space, model_id, "architecture", SEEDED, "arch-3")
    _idea(space, model_id, "data", SEEDED, "data-1")

    r1 = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose1"]))
    assert r1["status"] == "rejected"
    # a human lands a harness change outside any trial's diff and records it, so trial 2
    # resets the streak to zero before counting its own (non-improving) result -> 1, not 2.
    record_out_of_diff_change(space, model_id, author="alice")
    r2 = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose2"]))
    assert r2["status"] == "rejected"
    assert axis_streak(space, model_id)["non_improving_streak"] == 1

    # so trial 3 still draws from "architecture" -- the reset bought it one more trial.
    r3 = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose3"]))
    assert r3["candidate"] == third_arch


def test_an_out_of_diff_change_mark_must_carry_a_non_empty_author():
    space, model_id = _space_with_model()
    with pytest.raises(RegistryValidationError):
        record_out_of_diff_change(space, model_id, author="")


def test_a_second_out_of_diff_reset_on_the_same_axis_run_does_not_reset_again():
    space, model_id = _space_with_model(max_trials=100)
    for i in range(4):
        _idea(space, model_id, "architecture", SEEDED, f"arch-{i}")
    data_idea = _idea(space, model_id, "data", SEEDED, "data-1")

    dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose1"]))          # streak=1
    record_out_of_diff_change(space, model_id, author="alice")
    dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose2"]))          # reset used -> streak=1
    record_out_of_diff_change(space, model_id, author="alice")
    dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose3"]))          # 2nd mark ignored -> streak=2
    assert axis_streak(space, model_id)["non_improving_streak"] == 2

    # the 2nd mark bought no further reset, so the rabbit-hole intervention fires now.
    r4 = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose4"]))
    assert r4["candidate"] == data_idea


def test_an_out_of_diff_change_in_the_dispatcher_payload_never_renews_the_reset_budget():
    """The rabbit-hole watchdog judges the dispatcher, so nothing the dispatcher puts in its
    own trial payload may suppress it -- ``out_of_diff_change`` included. A dispatcher
    claiming one on every trial gets no reset at all, and the watchdog fires on schedule."""
    space, model_id = _space_with_model(max_trials=100)
    _idea(space, model_id, "architecture", SEEDED, "arch-1")
    _idea(space, model_id, "architecture", SEEDED, "arch-2")
    third_arch = _idea(space, model_id, "architecture", SEEDED, "arch-3")
    data_idea = _idea(space, model_id, "data", SEEDED, "data-1")

    def self_flagging_dispatcher(commit):
        def dispatcher(space, model, idea):
            return {"commit": commit, "out_of_diff_change": True}
        return dispatcher

    dispatch_trial(space, model_id, LEDGER, self_flagging_dispatcher("lose1"))
    dispatch_trial(space, model_id, LEDGER, self_flagging_dispatcher("lose2"))

    # the claim never reached the trial fact at all, and bought no reset ...
    trials = [t for t in space.list_facts("trial") if t.meta.get("model_id") == model_id]
    assert all("out_of_diff_change" not in t.meta for t in trials)
    assert axis_streak(space, model_id)["non_improving_streak"] == 2

    # ... so the rabbit-hole intervention fires and the next draw leaves "architecture".
    r3 = dispatch_trial(space, model_id, LEDGER, self_flagging_dispatcher("lose3"))
    assert r3["candidate"] == data_idea
    assert r3["candidate"] != third_arch


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


def test_axis_coverage_with_no_retrieval_axis_available_still_fires_the_rabbit_hole_guard():
    """Getting MORE stuck must not disable the weaker guard: at a same-axis streak of 5 with
    no retrieval axis left in the backlog, the axis-coverage branch falls through to the
    rabbit-hole check instead of returning empty-handed, so the stuck axis is still excluded.
    (Before the fix the draw stayed on "architecture" forever -- non-monotonic: a streak of
    2-4 WOULD have excluded it.)"""
    space, model_id = _space_with_model(max_trials=100)
    for i in range(6):
        _idea(space, model_id, "architecture", SEEDED, f"arch-{i}")

    # Five losing architecture trials. The rabbit-hole exclusion is UNSATISFIABLE each time
    # (architecture is the only axis with untried backlog), so the run reaches 5 with no
    # keep-pushing marker anywhere in sight.
    for commit in [f"lose{i}" for i in range(1, 6)]:
        dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher([commit]))
    assert axis_streak(space, model_id) == {
        "axis": "architecture", "same_axis_streak": 5, "non_improving_streak": 5
    }

    # a later ideation pass replenishes the backlog on a NON-retrieval axis: the exclusion is
    # satisfiable again, but there is still no retrieval axis for the axis-coverage branch.
    data_idea = _idea(space, model_id, "data", SEEDED, "data-1")

    r6 = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose6"]))
    assert r6["forced_axis"] is None  # no retrieval axis to force onto ...
    assert r6["candidate"] == data_idea  # ... but the rabbit-hole exclusion still fired


def test_an_auto_fired_forced_axis_never_overrides_a_caller_supplied_exclusion():
    """"A caller-supplied intervention always takes precedence": the axis-coverage escape
    valve reaches for the next retrieval axis the caller left open rather than the excluded
    one."""
    space, model_id = _space_with_model(max_trials=100)
    for i in range(6):
        _idea(space, model_id, "architecture", SEEDED, f"arch-{i}")

    exclusion = (Intervention(kind="exclude_axis", axis="current_code"),)
    for commit in [f"lose{i}" for i in range(1, 6)]:
        dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher([commit]), interventions=exclusion)
    assert axis_streak(space, model_id)["same_axis_streak"] == 5

    excluded_retrieval_idea = _idea(space, model_id, "current_code", SEEDED, "excluded by the caller")
    open_retrieval_idea = _idea(space, model_id, "prior_trials", SEEDED, "still permitted")

    r6 = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose6"]), interventions=exclusion)
    assert r6["forced_axis"] == "prior_trials"
    assert r6["candidate"] == open_retrieval_idea
    assert r6["candidate"] != excluded_retrieval_idea


# --- discovered-idea budget, per-trial budget, and the declared win condition ---


def test_an_exhausted_discovered_idea_budget_closes_the_campaign_instead_of_raising():
    """max_discovered_ideas is a cost control, and hitting it is a CLOSE condition, not a
    crash: register_idea's refusal must not escape supervise_campaign."""
    space, model_id = _space_with_model(max_trials=100, max_discovered_ideas=1)

    def idea_generator(space, model_id, forced_axis, permitted_axes):
        return {"axis": "optimizer", "description": "one more idea", "basis": "reasoned"}

    outcome = supervise_campaign(
        space, model_id, LEDGER, _scripted_dispatcher(["lose1", "lose2"]), idea_generator=idea_generator
    )
    assert outcome["close"] == CLOSE_BACKLOG_EXHAUSTED
    # exactly one discovered idea was registered -- the budget -- and the second dispatch
    # simply found no candidate.
    discovered = [f for f in space.list_facts("idea") if f.meta.get("origin") == DISCOVERED]
    assert len(discovered) == 1
    assert outcome["history"][-1]["candidate"] is None
    assert space.get(model_id).meta[CAMPAIGN_STATUS_FIELD] == "completed"


def test_a_dispatcher_overrunning_per_trial_seconds_closes_the_campaign():
    """per_trial_seconds (default 420) is a real bound, not a docstring: the overrun run is
    not registered as a trial and the campaign closes on it."""
    space, model_id = _space_with_model(max_trials=100)
    _idea(space, model_id, "architecture", SEEDED, "slow worker")

    ticks = iter([0.0, 421.0])
    outcome = supervise_campaign(
        space, model_id, LEDGER, _scripted_dispatcher(["lose1"]), clock=lambda: next(ticks)
    )
    assert outcome["close"] == CLOSE_TRIAL_TIMEOUT
    assert outcome["history"][0]["status"] == TRIAL_STATUS_TIMED_OUT
    assert outcome["history"][0]["trial_id"] is None
    assert space.list_facts("trial") == []


def test_a_dispatch_within_per_trial_seconds_is_unaffected():
    space, model_id = _space_with_model(max_trials=1)
    _idea(space, model_id, "architecture", SEEDED, "prompt worker")
    ticks = iter([0.0, 419.0])
    outcome = supervise_campaign(
        space, model_id, LEDGER, _scripted_dispatcher(["lose1"]), clock=lambda: next(ticks)
    )
    assert outcome["close"] == CLOSE_MAX_TRIALS


def test_first_adoption_does_not_win_a_campaign_whose_declared_target_it_misses():
    """The declared win_condition is evaluated against the LEDGER, not stubbed out by
    "the first adoption wins": with a target of val_bpb <= 0.3, the c1 adoption (0.5)
    advances the baseline but does NOT close the campaign; the later 0.2 adoption does."""
    space, model_id = _space_with_model(max_trials=10, win_condition={"metric_at_most": 0.3})
    first = _idea(space, model_id, "architecture", SEEDED, "improves, but not to target")
    second = _idea(space, model_id, "architecture", SEEDED, "reaches the target")

    ledger = dict(LEDGER)
    ledger["c-target"] = LedgerRow(value=0.2, throughput=1200, diff_lines=100)

    outcome = supervise_campaign(space, model_id, ledger, _scripted_dispatcher(["c1", "c-target"]))
    assert [h["status"] for h in outcome["history"]] == ["adopted", "adopted"]
    assert outcome["close"] == CLOSE_WON
    assert space.get(first).meta["status"] == STATUS_SUPERSEDED
    assert space.get(second).meta["status"] == STATUS_ADOPTED


def test_a_campaign_whose_win_condition_cannot_be_evaluated_is_refused_naming_it():
    space, model_id = _space_with_model(win_condition="reach val_bpb <= 0.80 eventually")
    _idea(space, model_id, "architecture", SEEDED, "arch-1")
    with pytest.raises(RegistryValidationError) as excinfo:
        supervise_campaign(space, model_id, LEDGER, _scripted_dispatcher(["c1"]))
    assert excinfo.value.field == "win_condition"
    assert space.list_facts("trial") == []  # refused before any compute was spent


@pytest.mark.parametrize(
    "declared,expected",
    [
        ({"metric_at_most": 0.8}, {"kind": "metric_at_most", "threshold": 0.8}),
        ({"metric_at_least": 12}, {"kind": "metric_at_least", "threshold": 12.0}),
        ("metric_at_most: 0.8", {"kind": "metric_at_most", "threshold": 0.8}),
        ("metric_at_least 12", {"kind": "metric_at_least", "threshold": 12.0}),
        ("beats baseline by noise_floor", {"kind": "beats baseline by noise_floor"}),
    ],
)
def test_parse_win_condition_accepts_the_structured_forms(declared, expected):
    assert parse_win_condition(declared) == expected


@pytest.mark.parametrize("declared", ["", "as good as possible", None, {"metric_at_most": "soon"}, 7])
def test_parse_win_condition_refuses_anything_unevaluable(declared):
    with pytest.raises(RegistryValidationError) as excinfo:
        parse_win_condition(declared)
    assert excinfo.value.field == "win_condition"


def test_the_bare_adoption_string_is_refused_unless_the_model_declares_it():
    """(P5) Only bootstrap ever checked a win condition -- build_model_meta refuses the bare
    string, so does the win_condition_declared precondition, and --win-condition is a required
    flag. register_model, register_model_with_baseline and both CLI verbs never looked: schema
    only asks that the key be non-empty. So a campaign stood up outside bootstrap was one
    string away from closing WON on its first adopted trial, which was reproduced end to end.
    The string keeps working -- it is what these fixtures and ball_campaign mean -- but meaning
    it now has to be said."""
    space, model_id = _space_with_model()
    del space.get(model_id).meta["win_on_adoption_ok"]

    with pytest.raises(RegistryValidationError) as excinfo:
        check_win_on_adoption_declared(space.get(model_id))
    assert excinfo.value.field == "win_condition"
    assert "win_on_adoption_ok" in str(excinfo.value)

    space.get(model_id).meta["win_on_adoption_ok"] = True
    check_win_on_adoption_declared(space.get(model_id))


@pytest.mark.parametrize("declared", [{"metric_at_most": 0.8}, "metric_at_least 12"])
def test_a_declared_numeric_target_needs_no_opt_in(declared):
    space, model_id = _space_with_model(win_condition=declared)
    del space.get(model_id).meta["win_on_adoption_ok"]
    check_win_on_adoption_declared(space.get(model_id))


def test_supervise_campaign_warns_but_still_resumes_an_undeclared_first_adoption_campaign():
    """The refusal belongs in the campaign loop's preflight, before trial one. Here it can only
    warn: supervise_campaign is also how an in-flight campaign RESUMES -- nothing distinguishes a
    resume from a fresh start -- and a campaign already several trials deep must not be bricked
    by a declaration it can no longer make retroactively."""
    space, model_id = _space_with_model()
    del space.get(model_id).meta["win_on_adoption_ok"]
    _idea(space, model_id, "architecture", SEEDED, "arch-1")

    with pytest.warns(UserWarning, match="win_on_adoption_ok") as warned:
        outcome = supervise_campaign(space, model_id, LEDGER, _scripted_dispatcher(["c1"]))
    assert outcome["close"] == CLOSE_WON
    assert model_id in str(warned[0].message)


def test_a_maximizing_campaign_wins_only_at_its_declared_floor():
    space, model_id = _space_with_model(
        direction="maximize", max_trials=10, win_condition={"metric_at_least": 100.0}
    )
    _idea(space, model_id, "architecture", SEEDED, "small gain")
    _idea(space, model_id, "architecture", SEEDED, "reaches the floor")

    ledger = {
        BASELINE_COMMIT: LedgerRow(value=1.0, throughput=1200, diff_lines=0),
        "up-a": LedgerRow(value=50.0, throughput=1200, diff_lines=100),
        "up-b": LedgerRow(value=140.0, throughput=1200, diff_lines=100),
    }
    outcome = supervise_campaign(space, model_id, ledger, _scripted_dispatcher(["up-a", "up-b"]))
    assert [h["status"] for h in outcome["history"]] == ["adopted", "adopted"]
    assert outcome["close"] == CLOSE_WON


def test_axis_streak_is_empty_with_no_trials_and_recomputed_fresh_not_cached():
    space, model_id = _space_with_model()
    assert axis_streak(space, model_id) == {"axis": None, "same_axis_streak": 0, "non_improving_streak": 0}
    _idea(space, model_id, "architecture", SEEDED, "arch-1")
    dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["c1"]))
    # an adopted (improving) trial resets the non-improving streak to zero.
    assert axis_streak(space, model_id) == {"axis": "architecture", "same_axis_streak": 1, "non_improving_streak": 0}


def test_a_model_may_raise_its_own_non_improving_streak_trigger():
    """(S3) A wide stage -- here 4 representation arms -- is abandoned after 2 misses at the
    default trigger. A model that sets ``non_improving_streak_trigger`` on its own meta
    raises the bar for ITSELF only, so the stage keeps drawing its untried arms."""
    space, model_id = _space_with_model(max_trials=100, non_improving_streak_trigger=4)
    rep_ideas = [_idea(space, model_id, "representation", SEEDED, f"rep-{i}") for i in range(4)]
    other = _idea(space, model_id, "data", SEEDED, "data-1")

    drawn = []
    for commit in ("lose1", "lose2", "lose3"):
        drawn.append(dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher([commit]))["candidate"])

    # At the default trigger of 2 the third draw would have escaped to "data"; at 4 it stays.
    assert drawn == rep_ideas[:3]
    assert other not in drawn
    assert axis_streak(space, model_id)["non_improving_streak"] == 3

    # The raised trigger still FIRES, it is only later: the 4th miss excludes the axis.
    fourth = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose4"]))
    assert fourth["candidate"] == rep_ideas[3]
    fifth = dispatch_trial(space, model_id, LEDGER, _scripted_dispatcher(["lose5"]))
    assert fifth["candidate"] == other


def test_the_non_improving_streak_trigger_defaults_to_two_when_a_model_sets_none():
    """(S3) Opt-in only: a model with no ``non_improving_streak_trigger`` on its meta keeps
    the :data:`NON_IMPROVING_STREAK_TRIGGER` default, so an already-running campaign is
    bit-identical."""
    space, model_id = _space_with_model(max_trials=100)
    assert "non_improving_streak_trigger" not in space.get(model_id).meta
    assert model_non_improving_trigger(space.get(model_id).meta) == NON_IMPROVING_STREAK_TRIGGER
