"""Table-driven trial verdict + baseline ratchet (R10).

Builds on R3's idea lifecycle (:mod:`knowledge.ml_registry.lifecycle`) and R12's noise
floor (:mod:`knowledge.ml_registry.floor`). :func:`adjudicate_verdict` is the full
adjudication a trial goes through once its idea is claimed and run: it joins the trial's
``commit`` (and the model's current ``baseline`` commit) against an external ledger of
per-commit rows -- metric value, throughput, and net diff lines -- and decides ONE of
four verdicts:

The stagnant band is CLOSED on both sides: ``-noise_floor <= delta <= noise_floor`` is
stagnant. The floor is one standard deviation of the baseline runs, so a delta of exactly
one floor is not evidence of anything in EITHER direction -- adoption needs
``delta > noise_floor`` and rejection needs ``delta < -noise_floor``, both strict.

* ``"adopted"``  -- the trial's ledger value beats the current baseline by MORE than one
  ``noise_floor`` in the model's improving direction. The model's ``baseline`` advances to
  the trial's commit, the commit it replaces is retained as ``previous_baseline``, and the
  idea is adopted (:func:`~knowledge.ml_registry.lifecycle.adopt_idea`). Any PRIOR adoption
  for the model is superseded
  (:func:`~knowledge.ml_registry.lifecycle.supersede_adoption`), not invalidated: it was a
  real bar while it stood, so the ideas rejected during its tenure stay rejected.
* ``"parked"``   -- the delta is within one ``noise_floor`` of the baseline, inclusive
  (stagnant), and the trial's recomputed ``diff_lines`` is within the model's
  ``diff_size_limit`` (its net-line bound). The idea is parked
  (:func:`~knowledge.ml_registry.lifecycle.park_idea`).
* ``"rejected"`` -- either the trial's ledger value falls MORE than one ``noise_floor``
  below baseline in the worsening direction, or it is stagnant but breaches the net-line
  bound. The idea is rejected (:func:`~knowledge.ml_registry.lifecycle.reject_idea`).
* ``"voided"``   -- the trial's recomputed throughput falls more than
  :data:`THROUGHPUT_FLOOR_FRACTION` (5%) below the model's registered
  ``baseline_throughput``, which is read as a SPEED only when
  :data:`~knowledge.ml_registry.floor.BASELINE_THROUGHPUT_UNITS_FIELD` says it is one. The
  run is unreliable on its face: no adjudication happens at all (no idea-state change), the
  trial is marked ``"voided"`` for a re-run.

Before any of that, a trial's SELF-REPORTED ``throughput``/``diff_lines`` (recorded on the
trial at registration time) is checked against the authoritative ledger row for its own
commit -- a disagreement is refused naming the disagreeing field, the same
recompute-refuses-drift shape R12's :func:`~knowledge.ml_registry.floor.register_model_with_baseline`
uses for a model's stored floor/throughput.

RATCHET: only a REJECTED verdict caused by the worsening-direction noise-floor breach (not
a stagnant/diff-bound rejection) advances the model's consecutive-rejection ratchet
(``ratchet_count`` plus the distinct idea ids behind it, ``rejection_streak_ideas``). The
moment the last 3 entries of that streak name 3 DISTINCT ideas, the model's last adoption
is INVALIDATED through R3's :func:`~knowledge.ml_registry.lifecycle.invalidate_adoption`
and its baseline restored to ``previous_baseline``. 3 consecutive rejections on distinct
ideas are the evidence that the adoption was noise and the baseline it set was FALSE --
so every idea rejected while that false bar stood, the streak's own rejections included,
was judged against a bar that never existed and is RE-QUEUED to the untried backlog. The
ratchet counter and streak reset either way.

A rejection joins that streak only when it is ATTRIBUTABLE to the adoption: the trial would
have PARKED or WON against ``previous_baseline`` -- the bar the adoption replaced -- and so
lost only because the adoption raised the bar. A trial that loses against the OLD bar too is
simply a worse arm and says nothing about whether the adoption was real, so it is SKIPPED: it
neither joins the streak nor disturbs the one already there. Nothing else resets the ratchet:
an adoption resets it (a fresh baseline earns a fresh streak), an invalidation resets it
(fired or not -- when there is no active adoption to invalidate, the rule is a no-op that
still consumes/resets the streak), and any other verdict leaves it untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

from knowledge.ml_registry.guards import ADJUDICATION_SOURCE
from knowledge.ml_registry.floor import (
    BASELINE_THROUGHPUT_UNITS_FIELD,
    RATCHET_COUNT_FIELD,
    REJECTION_STREAK_FIELD,
    THROUGHPUT_UNITS_METRIC_MEAN,
)
from knowledge.ml_registry.lifecycle import (
    TRIAL_STATUS_SUCCEEDED,
    active_adoption,
    adopt_idea,
    invalidate_adoption,
    park_idea,
    reject_idea,
    supersede_adoption,
)
from knowledge.ml_registry.schema import MODEL, TRIAL, RegistryValidationError
from knowledge.ml_registry.write_path import Fact, RegistrySpace, mutate_model

VERDICT_ADOPTED = "adopted"
VERDICT_PARKED = "parked"
VERDICT_REJECTED = "rejected"
VERDICT_VOIDED = "voided"

#: A run in any other ledger status is UNFAIR, not losing, and is voided rather than adjudicated.
#: The trainer writes this column precisely to say so -- `budget_exhausted` marks a run cut short
#: by wall clock, and scoring an under-trained model as a rejection records a settled answer to a
#: question that was never actually asked.
FAIR_RUN_STATUSES: frozenset[str] = frozenset({"ok", ""})

BASELINE_FIELD = "baseline"
PREVIOUS_BASELINE_FIELD = "previous_baseline"

DEFAULT_REACTIVATION_TRIGGER = "revisit once a new idea or a harness change is available"

# A trial's recomputed throughput must not fall more than this fraction below the model's
# registered baseline_throughput, or it is voided (re-run) rather than adjudicated at all.
THROUGHPUT_FLOOR_FRACTION = 0.05

# A trial rejected consecutively on 3 distinct ideas fires the ratchet.
RATCHET_STREAK_LENGTH = 3

#: The axis the rejection streak was probing, under the axis-reset rule that
#: counterfactual attribution replaced (see the ratchet block in :func:`adjudicate_verdict`).
#: No longer written; still CLEARED alongside the streak so a model carrying one from an
#: in-flight campaign does not leave a field behind that reads as live state.
REJECTION_STREAK_AXIS_FIELD = "rejection_streak_axis"

_AGREEMENT_TOLERANCE = 1e-9


@dataclass(frozen=True)
class LedgerRow:
    """One external-ledger row for a commit: its scored metric value plus the throughput
    and net diff-line count the run actually produced."""

    value: float
    throughput: float
    diff_lines: float
    #: The loop's own verdict on whether this run was FAIR. Anything outside `FAIR_RUN_STATUSES`
    #: means the number in `value` was not produced under the conditions the arm was meant to be
    #: measured under, so it cannot be adjudicated. Defaults to "ok" so a ledger that does not
    #: record status behaves exactly as before.
    status: str = "ok"


def _agree(a: float, b: float) -> bool:
    return abs(a - b) <= _AGREEMENT_TOLERANCE


def _trial(space: RegistrySpace, trial_id: str) -> Fact:
    trial = space.get(trial_id)
    if trial is None or trial.category != TRIAL:
        raise RegistryValidationError(f"trial {trial_id!r} was never registered", field="trial_id")
    return trial


def _model(space: RegistrySpace, model_id: str) -> Fact:
    model = space.get(model_id)
    if model is None or model.category != MODEL:
        raise RegistryValidationError(
            f"trial references model {model_id!r} that was never registered", field="model_id"
        )
    return model


def _ledger_row(ledger_rows: dict[str, LedgerRow], commit: str, *, field: str) -> LedgerRow:
    row = ledger_rows.get(commit)
    if row is None:
        raise RegistryValidationError(
            f"commit {commit!r} has no matching row in the external ledger", field=field
        )
    return row


def _reset_ratchet(model: Fact) -> None:
    model.meta[RATCHET_COUNT_FIELD] = 0
    model.meta[REJECTION_STREAK_FIELD] = []
    model.meta.pop(REJECTION_STREAK_AXIS_FIELD, None)


def _delta_against(direction: str, baseline_value: float, value: float) -> float:
    """How far ``value`` beats ``baseline_value`` in the model's improving direction."""
    return baseline_value - value if direction == "minimize" else value - baseline_value


def _attributable_to_the_adoption(
    model: Fact,
    ledger_rows: dict[str, LedgerRow],
    value: float,
    *,
    direction: str,
    noise_floor: float,
) -> bool:
    """Would this rejected trial have PARKED or WON against the bar the adoption replaced?

    Only then is the loss explicable by the adoption having raised the bar, which is the
    one inference the ratchet makes. When the question cannot be ASKED -- no
    ``previous_baseline`` (no adoption this module made is standing, so an invalidation
    would be a no-op anyway), or no ledger row for it -- the answer is yes: an unanswerable
    question must leave the guard where it was, not quietly switch it off.
    """
    previous_commit = model.meta.get(PREVIOUS_BASELINE_FIELD)
    if previous_commit is None:
        return True
    previous_row = ledger_rows.get(str(previous_commit))
    if previous_row is None:
        return True
    return _delta_against(direction, previous_row.value, value) >= -noise_floor


def _invalidate_ratchet(space: RegistrySpace, model: Fact, model_id: str, reason: str) -> None:
    """Invalidate the model's last adoption and restore its previous baseline.

    The streak proves the adoption was noise, so its baseline was false -- every idea
    rejected during its tenure was measured against a bar that never existed and is
    re-queued to the untried backlog by
    :func:`~knowledge.ml_registry.lifecycle.invalidate_adoption`. A no-op (beyond resetting
    the streak) when nothing is adopted.
    """
    adopted = active_adoption(space, model_id)
    if adopted is not None:
        invalidate_adoption(space, adopted.id, reason)
        previous = model.meta.pop(PREVIOUS_BASELINE_FIELD, None)
        if previous is not None:
            mutate_model(space, model_id, {BASELINE_FIELD: previous}, source=ADJUDICATION_SOURCE)
    _reset_ratchet(model)


def adjudicate_verdict(
    space: RegistrySpace,
    trial_id: str,
    ledger_rows: dict[str, LedgerRow],
    *,
    reactivation_trigger: str = DEFAULT_REACTIVATION_TRIGGER,
) -> str:
    """Decide and apply a trial's full verdict. Returns one of :data:`VERDICT_ADOPTED`,
    :data:`VERDICT_PARKED`, :data:`VERDICT_REJECTED`, :data:`VERDICT_VOIDED`."""
    trial = _trial(space, trial_id)
    model_id = str(trial.meta.get("model_id"))
    model = _model(space, model_id)
    idea_id = str(trial.meta.get("idea_id"))
    commit = str(trial.meta.get("commit"))

    row = _ledger_row(ledger_rows, commit, field="commit")

    self_throughput = trial.meta.get("throughput")
    self_diff_lines = trial.meta.get("diff_lines")
    if self_throughput is None:
        raise RegistryValidationError(
            f"trial {trial_id!r} has no self-reported throughput to verify", field="throughput"
        )
    if self_diff_lines is None:
        raise RegistryValidationError(
            f"trial {trial_id!r} has no self-reported diff_lines to verify", field="diff_lines"
        )
    if not _agree(float(self_throughput), row.throughput):
        raise RegistryValidationError(
            f"trial {trial_id!r} self-reported throughput {self_throughput!r} disagrees with the "
            f"ledger's recomputed throughput {row.throughput!r} for commit {commit!r}",
            field="throughput",
        )
    if not _agree(float(self_diff_lines), row.diff_lines):
        raise RegistryValidationError(
            f"trial {trial_id!r} self-reported diff_lines {self_diff_lines!r} disagrees with the "
            f"ledger's recomputed diff_lines {row.diff_lines!r} for commit {commit!r}",
            field="diff_lines",
        )

    baseline_commit = str(model.meta.get(BASELINE_FIELD))
    baseline_row = _ledger_row(ledger_rows, baseline_commit, field=BASELINE_FIELD)

    # An unfair run is voided before any comparison. The ledger's own status column exists to say
    # the number was not produced under the intended conditions -- `budget_exhausted` means wall
    # clock cut the run short. Adjudicating it anyway records a REJECTION for a question that was
    # never asked, and a rejection is exactly what a future session reads as settled.
    #
    # Observed on the first campaign to run a genuinely expensive arm: a graph model was cut off by
    # the time budget, its per-seed scores degrading 0.618 / 0.627 / 0.412 / 0.049 as it diverged,
    # and the registry scored the truncated mean as a -0.2766 rejection of the entire model family.
    # LedgerRow did not carry `status`, so this could not be seen at all.
    if str(row.status).strip().lower() not in FAIR_RUN_STATUSES:
        trial.meta["status"] = VERDICT_VOIDED
        trial.meta["void_reason"] = f"ledger status {row.status!r} is not a fair run"
        return VERDICT_VOIDED

    baseline_throughput = float(model.meta["baseline_throughput"])
    raw_fraction = model.meta.get("void_throughput_fraction", THROUGHPUT_FLOOR_FRACTION)
    try:
        void_fraction = float(raw_fraction)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        void_fraction = THROUGHPUT_FLOOR_FRACTION
    if void_fraction > 0:
        # The speed void may only fire against a bar that MEASURES SPEED. `baseline_throughput`
        # holds one of two incompatible things and R12 stamps WHICH in
        # `baseline_throughput_units`: the slowest rows/sec of the baseline runs when
        # registration was given ledger throughputs, the MEAN OF THE BASELINE METRIC VALUES when
        # it was not. `floor._metric_baseline` already refuses to read a rows_per_sec value as a
        # metric bar; this module ran the SAME category error in the opposite direction, because
        # it never consulted the stamp at all.
        #
        # Reproduced: a metric_mean-stamped model whose baseline metric mean is 0.90 and whose
        # real ledger throughput is 0.5 rows/sec had every trial voided -- "throughput 0.5 is
        # more than 5% below baseline_throughput 0.9" is a METRIC MEAN being used as a speed
        # limit -- until the void limit closed the campaign without adjudicating a single arm.
        #
        # An explicit metric_mean stamp REFUSES rather than skipping. The registry knows the
        # stored number cannot bound a speed, so a campaign that asked for a speed gate has
        # asked for one it has no bar for, and adjudicating on regardless would leave it
        # believing a guard is running that is not. Both remedies are named in the refusal, and
        # a campaign that never wanted the gate reaches neither: void_throughput_fraction=0 is
        # checked first.
        #
        # A model with NO stamp keeps the gate. The stamp is recent, and plain `register_model`
        # -- the path the supervisor's own campaigns take -- never writes one while its callers
        # pass a real rows/sec bar (1200 rows/sec against a val_bpb of 1.0, 3.38 against an F1
        # of 0.70). Skipping the void for every unstamped model would retire a real guard on all
        # of them to fix a case none of them are in. It does leave one hole open: a model
        # registered through `register_model_with_baseline` BEFORE the stamp existed carries a
        # metric mean with no stamp to say so, and this gate will still misfire on it. The fix
        # for that model is to re-register it so the stamp exists, which is the same remedy
        # `floor._metric_baseline` names for the mirror-image error.
        if model.meta.get(BASELINE_THROUGHPUT_UNITS_FIELD) == THROUGHPUT_UNITS_METRIC_MEAN:
            raise RegistryValidationError(
                f"model {model_id!r} records baseline_throughput {baseline_throughput!r} as "
                f"{THROUGHPUT_UNITS_METRIC_MEAN!r} -- the mean of the baseline runs' METRIC "
                "values, which cannot bound a throughput -- so the speed void has no bar to "
                "fire against: re-register the model through register-model-with-baseline "
                "against a ledger that measures throughput (which stamps rows_per_sec), or set "
                "void_throughput_fraction=0 to run this campaign without a speed void",
                field=BASELINE_THROUGHPUT_UNITS_FIELD,
            )
        if row.throughput < baseline_throughput * (1 - void_fraction):
            trial.meta["status"] = VERDICT_VOIDED
            trial.meta["void_reason"] = (
                f"throughput {row.throughput} is more than {void_fraction:.0%} below "
                f"baseline_throughput {baseline_throughput}"
            )
            return VERDICT_VOIDED

    direction = model.meta.get("direction")
    if direction not in ("minimize", "maximize"):
        raise RegistryValidationError(
            f"model direction must be 'minimize' or 'maximize', got {direction!r}", field="direction"
        )
    direction = str(direction)
    delta = _delta_against(direction, baseline_row.value, row.value)

    noise_floor = float(model.meta["noise_floor"])
    diff_size_limit = float(model.meta["diff_size_limit"])

    if delta > noise_floor:
        trial.meta["status"] = TRIAL_STATUS_SUCCEEDED
        mutate_model(
            space,
            model_id,
            {PREVIOUS_BASELINE_FIELD: baseline_commit, BASELINE_FIELD: commit},
            source=ADJUDICATION_SOURCE,
        )
        prior = active_adoption(space, model_id)
        if prior is not None and prior.id != idea_id:
            # Superseded, NOT invalidated: the prior adoption was a real bar while it stood,
            # so the ideas rejected under its tenure stay rejected.
            supersede_adoption(space, prior.id, f"superseded by trial {trial_id}")
        adopt_idea(space, idea_id, trial_id)
        _reset_ratchet(model)
        return VERDICT_ADOPTED

    # Symmetric with the strict `delta > noise_floor` adoption test above: a delta of exactly
    # one floor is one standard deviation, i.e. no evidence, in EITHER direction.
    if delta < -noise_floor:
        trial.meta["status"] = "failed"
        reject_idea(space, idea_id, "trial fell more than one noise-floor standard deviation below the current baseline")
        # COUNTERFACTUAL ATTRIBUTION. The streak is evidence ABOUT THE ADOPTION, so a
        # rejection joins it only when the adoption is what caused the rejection: the trial
        # would have PARKED or WON against `previous_baseline`, the bar the adoption replaced,
        # and lost only because that bar was raised. A trial that loses against the OLD bar too
        # is a worse arm and nothing more -- it carries no information about whether the
        # adoption was real, and counting it is how the ratchet reverts genuine wins.
        #
        # This REPLACES a pair of proxies for that question -- a depth threshold
        # (MATERIAL_REJECTION_FLOORS, 2 sigma below baseline) and a skip on a change of `axis`
        # -- both of which were reproduced failing at it:
        #  * a real +10-floor adoption followed by three deep exploratory losers (-20 floors,
        #    three distinct axes) that ALSO lost against the pre-adoption baseline cleared the
        #    depth bar on every one and reverted the win, on evidence that said nothing about
        #    the adoption at all;
        #  * the axis skip pinned the streak at 1 forever in a wide campaign with <=2 arms per
        #    axis, and the supervisor's own rabbit-hole watchdog (supervisor.py's
        #    NON_IMPROVING_STREAK_TRIGGER) EXCLUDES an axis after 2 non-improving trials, so it
        #    was actively removing the third same-axis rejection the ratchet was waiting for.
        # The depth line was also wrong in KIND for the campaigns this registry now serves:
        # association / detection / contact_point / court-marking all have DETERMINISTIC
        # incumbents with bootstrap-resampled floors, where a -1.5-floor rejection is an exactly
        # measured regression with no run wobble to excuse it away, while an arm 50 floors down
        # is just a bad idea. The threshold read both of those exactly the wrong way round.
        # Attribution needs no such constant: it asks the ratchet's own question directly.
        #
        # NOTE what this costs, because it is not nothing. The staged incident recorded in
        # `reset_ratchet`'s docstring -- an adopted representation change, then an MLP and a
        # transformer that both scored ABOVE the pre-adoption baseline and would merely have
        # parked against it -- DOES accumulate under this rule, since parking against the old
        # bar and rejecting against the new one is precisely the shape attribution counts. That
        # those arms varied a different stage is a fact only the caller holds, and
        # `reset_ratchet` remains the explicit, recorded way for the caller to say so.
        #
        # A SKIPPED rejection leaves the streak and its count exactly as it found them. Wiping
        # instead of skipping is what turned "this one does not count" into "forget everything
        # that did", which is how a false adoption used to survive a mixed run of attributable
        # and unattributable losses.
        if not _attributable_to_the_adoption(
            model, ledger_rows, row.value, direction=direction, noise_floor=noise_floor
        ):
            return VERDICT_REJECTED
        streak = list(model.meta.get(REJECTION_STREAK_FIELD) or [])
        streak.append(idea_id)
        model.meta[REJECTION_STREAK_FIELD] = streak
        model.meta[RATCHET_COUNT_FIELD] = len(streak)
        if len(streak) >= RATCHET_STREAK_LENGTH and len(set(streak[-RATCHET_STREAK_LENGTH:])) == RATCHET_STREAK_LENGTH:
            _invalidate_ratchet(
                space, model, model_id,
                f"ratchet: {RATCHET_STREAK_LENGTH} consecutive rejections on distinct ideas invalidated the adoption",
            )
        return VERDICT_REJECTED

    # stagnant band, closed on both sides: -noise_floor <= delta <= noise_floor
    if row.diff_lines <= diff_size_limit:
        trial.meta["status"] = "stagnant"
        park_idea(space, idea_id, reactivation_trigger)
        return VERDICT_PARKED

    trial.meta["status"] = "failed"
    reject_idea(space, idea_id, "stagnant trial breached the model's net-line bound")
    return VERDICT_REJECTED

def reset_ratchet(space: RegistrySpace, model_id: str, reason: str) -> dict[str, object]:
    """Clear a model's rejection streak without touching its baseline or any verdict.

    The ratchet reads three consecutive rejections as evidence that the last ADOPTION was noise --
    the reasoning being that a false adoption raises the bar, so the rejections it causes look
    ordinary. That inference holds only while the rejections are competing against the adoption on
    the same axis. It does not hold across a STAGE boundary, where later arms vary something else
    entirely.

    Observed on the first staged campaign: a representation change was adopted at +0.0239, then two
    architecture arms rejected -- an MLP at -0.0177 and a transformer at -0.0146. Neither rejection
    was caused by an inflated bar. BOTH scored ABOVE the pre-adoption baseline and would merely have
    parked against it; they lost because those architectures are worse on ~1,400 samples, which is
    exactly what one of them was authored to demonstrate. One more rejection from an unrelated
    augmentation arm would have rolled back a sound adoption and re-queued three settled ideas.

    The registry cannot detect a stage boundary itself -- stages are the caller's taxonomy (see
    `staging.py`) -- so this is the caller's call to make, and it is deliberately explicit rather
    than automatic. `adjudicate_verdict`'s own attribution test does not cover this case and is
    not meant to: both arms PARKED against the pre-adoption baseline and rejected against the new
    one, which is precisely the shape it counts as evidence. Which stage an arm varies is a fact
    only the caller holds, so saying so stays the caller's job, recorded here rather than inferred
    there.

    Baseline, previous_baseline and every recorded verdict are left untouched. This ONLY forgets
    the streak, so a genuinely false adoption remains catchable by the next three rejections that
    do compete with it.

    ``reason`` is required and recorded. A ratchet cleared without a stated reason is
    indistinguishable from one cleared to protect a result someone liked.
    """
    model = space.get(model_id)
    if model is None or model.category != MODEL:
        raise RegistryValidationError(
            f"model {model_id!r} was never registered", field="model_id")
    if not reason.strip():
        raise RegistryValidationError("a reset reason is required", field="reason")
    cleared = {
        "ratchet_count": int(model.meta.get(RATCHET_COUNT_FIELD) or 0),
        "rejection_streak_ideas": list(model.meta.get(REJECTION_STREAK_FIELD) or []),
    }
    model.meta[RATCHET_COUNT_FIELD] = 0
    model.meta[REJECTION_STREAK_FIELD] = []
    history = list(model.meta.get("ratchet_resets") or [])
    history.append({"reason": reason, **cleared})
    model.meta["ratchet_resets"] = history
    return cleared
