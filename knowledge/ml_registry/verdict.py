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
ratchet counter and streak reset either way. Nothing else resets the ratchet: an adoption
resets it (a fresh baseline earns a fresh streak), an invalidation resets it (fired or not
-- when there is no active adoption to invalidate, the rule is a no-op that still
consumes/resets the streak), and any other verdict leaves it untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

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
from knowledge.ml_registry.write_path import Fact, RegistrySpace

VERDICT_ADOPTED = "adopted"
VERDICT_PARKED = "parked"
VERDICT_REJECTED = "rejected"
VERDICT_VOIDED = "voided"

BASELINE_FIELD = "baseline"
PREVIOUS_BASELINE_FIELD = "previous_baseline"

DEFAULT_REACTIVATION_TRIGGER = "revisit once a new idea or a harness change is available"

# A trial's recomputed throughput must not fall more than this fraction below the model's
# registered baseline_throughput, or it is voided (re-run) rather than adjudicated at all.
THROUGHPUT_FLOOR_FRACTION = 0.05

# A trial rejected consecutively on 3 distinct ideas fires the ratchet.
RATCHET_STREAK_LENGTH = 3

_AGREEMENT_TOLERANCE = 1e-9


@dataclass(frozen=True)
class LedgerRow:
    """One external-ledger row for a commit: its scored metric value plus the throughput
    and net diff-line count the run actually produced."""

    value: float
    throughput: float
    diff_lines: float


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
            model.meta[BASELINE_FIELD] = previous
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

    baseline_throughput = float(model.meta["baseline_throughput"])
    if row.throughput < baseline_throughput * (1 - THROUGHPUT_FLOOR_FRACTION):
        trial.meta["status"] = VERDICT_VOIDED
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
        model.meta[PREVIOUS_BASELINE_FIELD] = baseline_commit
        model.meta[BASELINE_FIELD] = commit
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
