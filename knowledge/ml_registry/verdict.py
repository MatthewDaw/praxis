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
  ``baseline_throughput``. The run is unreliable on its face: no adjudication happens at
  all (no idea-state change), the trial is marked ``"voided"`` for a re-run.

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

The streak is ALSO reset when a MARGINAL rejection arrives on a different ``axis`` than the one
the streak has been probing: three rejections spread over three axes are three unrelated
experiments, and a stage-blind supervisor interleaves them as a matter of course, so counting
them as accumulated evidence against one adoption is the inference :func:`reset_ratchet` calls
invalid. A rejection more than :data:`MATERIAL_REJECTION_FLOORS` floors below baseline is exempt
-- real damage counts whatever axis found it, or the ratchet goes inert against exactly the bad
adoption it exists to roll back. Nothing else resets the ratchet: an adoption
resets it (a fresh baseline earns a fresh streak), an invalidation resets it (fired or not
-- when there is no active adoption to invalidate, the rule is a no-op that still
consumes/resets the streak), and any other verdict leaves it untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

from knowledge.ml_registry.guards import ADJUDICATION_SOURCE
from knowledge.ml_registry.floor import RATCHET_COUNT_FIELD, REJECTION_STREAK_FIELD
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

#: How many noise floors below baseline a rejection must fall to count as MATERIAL rather than
#: marginal. The floor is one standard deviation of the baseline runs, so this is the 2-sigma
#: line: a delta past it is not a plausible sampling wobble, it is real damage, and real damage
#: is evidence against the adoption no matter which axis found it. A rejection between one and
#: two floors below baseline is a loss but not a verdict on the adoption, so it is the one that
#: the axis reset is allowed to discard.
MATERIAL_REJECTION_FLOORS = 2.0

#: The axis the current rejection streak is probing. Stored ALONGSIDE (never inside)
#: :data:`~knowledge.ml_registry.floor.REJECTION_STREAK_FIELD` so the streak itself keeps its
#: stored shape -- a plain list of idea ids -- and a streak written before this field existed
#: stays readable by every reader, this module included.
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


def _idea_axis(space: RegistrySpace, idea_id: str) -> str:
    """The axis of the idea behind a trial -- the line of attack the trial is probing.

    An idea fact is REQUIRED to carry ``axis`` (see :data:`~knowledge.ml_registry.schema.
    REQUIRED_META_KEYS`), so a missing one means the idea was never registered; that is not
    this function's refusal to make, and an unknown axis simply reads as its own axis.
    """
    idea = space.get(idea_id)
    if idea is None:
        return ""
    return str(idea.meta.get("axis") or "")


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
    if void_fraction > 0 and row.throughput < baseline_throughput * (1 - void_fraction):
        trial.meta["status"] = VERDICT_VOIDED
        trial.meta["void_reason"] = (
            f"throughput {row.throughput} is more than {void_fraction:.0%} below "
            f"baseline_throughput {baseline_throughput}"
        )
        return VERDICT_VOIDED

    direction = model.meta.get("direction")
    if direction == "minimize":
        delta = baseline_row.value - row.value
    elif direction == "maximize":
        delta = row.value - baseline_row.value
    else:
        raise RegistryValidationError(
            f"model direction must be 'minimize' or 'maximize', got {direction!r}", field="direction"
        )

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
        # AXIS RESET, gated on materiality. The streak is evidence ABOUT THE ADOPTION only while
        # its rejections compete with the adoption on the same line of attack -- exactly the
        # argument `reset_ratchet` below makes about stage boundaries. `supervisor.py` has no
        # stage awareness whatsoever (`grep -c stage` returns 0), so it interleaves arms from
        # unrelated stages and cross-stage streaks are the NORMAL case, not an edge case: three
        # rejections from three different axes are three unrelated experiments that look, to a
        # stage-blind counter, identical to three probes of one hypothesis. The axis is the
        # closest thing to a stage this module can see without reaching into the supervisor, so
        # a change of axis starts a fresh streak.
        #
        # But a PURE axis reset makes the ratchet inert against the case it exists for. A
        # genuinely bad adoption degrades every downstream arm, and under a stage-blind
        # supervisor that evidence arrives round-robin -- data, architecture, optimization,
        # data, ... -- so a pure reset truncates the streak to 1 forever. Measured: six
        # consecutive rejections at 10x the floor, spread over three axes, never fired at all.
        # So the reset is gated on MATERIALITY. A rejection more than
        # MATERIAL_REJECTION_FLOORS (2 sigma) below baseline is real damage rather than a
        # sampling wobble and counts toward the streak whatever its axis; only a MARGINAL
        # rejection -- between one and two floors below -- is discarded on an axis change. That
        # is precisely the B3 case: a real +0.06 win erased by three arms sitting a hair below
        # it on three unrelated axes.
        #
        # Note the noise floor is the ONLY materiality test being applied here, and it is not
        # duplicated: a degenerate (near-zero) floor is refused at REGISTRATION, and adding a
        # second absolute threshold in this module would mask that refusal rather than help it.
        # MATERIAL_REJECTION_FLOORS scales with the model's own floor for the same reason.
        #
        # A streak carried over from before REJECTION_STREAK_AXIS_FIELD existed has no recorded
        # axis; it is ADOPTED by the current rejection rather than discarded, so an in-flight
        # campaign neither loses accumulated evidence nor has a phantom axis change invented
        # for it. A stale axis left behind by another module's reset is inert: an empty streak
        # always starts fresh.
        axis = _idea_axis(space, idea_id)
        material = delta < -MATERIAL_REJECTION_FLOORS * noise_floor
        streak = list(model.meta.get(REJECTION_STREAK_FIELD) or [])
        streak_axis = model.meta.get(REJECTION_STREAK_AXIS_FIELD)
        if streak and not material and streak_axis is not None and str(streak_axis) != axis:
            streak = []
        model.meta[REJECTION_STREAK_AXIS_FIELD] = axis
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
    than automatic.

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
