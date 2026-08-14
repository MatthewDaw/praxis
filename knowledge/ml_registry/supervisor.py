"""Campaign supervisor for the af-ml-research autoresearch loop (R8).

Drives ONE campaign -- one registered model's whole trial sequence -- to close, by
dispatching one worker per trial SERIALLY: :func:`dispatch_trial` is the unit of work a
fresh, independent worker session performs (register/attempt an idea, register its trial,
adjudicate it, apply the adjudication side effects) and :func:`supervise_campaign` calls it
repeatedly, one at a time, never concurrently, until a close condition fires.

Builds on R2's write path, R3/R4's idea lifecycle + query surface, and R11's campaign
budgets -- this module adds no new persistence primitive of its own; it only sequences
calls into those already-proven functions against the same JSON-persisted
:class:`~knowledge.ml_registry.write_path.RegistrySpace` stand-in the rest of the registry
uses.

Candidate selection order, per dispatch:

1. Resolve this dispatch's interventions (:func:`resolve_interventions`) fresh from the
   registry's current backlog -- never from anything cached across calls. A
   ``"forced_axis"`` intervention takes precedence over seed-first: the candidate is drawn
   from that axis alone. An ``"exclude_axis"`` intervention that would leave no untried
   idea outside the excluded axis is UNSATISFIABLE -- it is recorded as such and dropped
   (the axis stays permitted) before seed-first proceeds.
2. Seed-first: the earliest untried ``origin="seeded"`` idea on a permitted axis.
3. Otherwise the earliest untried ``origin="discovered"`` idea on a permitted axis.
4. Otherwise, when an ``idea_generator`` was supplied, ask it for one more idea; it is
   registered with an axis, a basis and ``origin="discovered"`` BEFORE its trial is ever
   recorded.
5. Otherwise there is no candidate -- the campaign's backlog is exhausted.

Nothing here is held in loop-local state across dispatches: every counter this module
reports (the ratchet count, which interventions are unsatisfiable, the close condition)
is RECOMPUTED from ``space`` (the registry) and ``ledger_rows`` (the external results
ledger) each time. A campaign interrupted between build rounds resumes by calling
:func:`supervise_campaign` again against the same space/ledger; nothing distinguishes that
resume from a fresh start, so a round boundary is never itself a timeout or a failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from knowledge.ml_registry.floor import CAMPAIGN_STATUS_FIELD
from knowledge.ml_registry.lifecycle import untried_backlog
from knowledge.ml_registry.schema import MODEL, TRIAL, RegistryValidationError
from knowledge.ml_registry.verdict import LedgerRow, VERDICT_ADOPTED, adjudicate_verdict
from knowledge.ml_registry.write_path import (
    DISCOVERED,
    MODEL_DEFAULTS,
    SEEDED,
    Fact,
    RegistrySpace,
    register_idea,
    register_trial,
)

FORCED_AXIS = "forced_axis"
EXCLUDE_AXIS = "exclude_axis"
INTERVENTION_KINDS: tuple[str, ...] = (FORCED_AXIS, EXCLUDE_AXIS)

TRIAL_STATUS_VOIDED = "voided"

CLOSE_WON = "won"
CLOSE_MAX_TRIALS = "max_trials_reached"
CLOSE_BACKLOG_EXHAUSTED = "backlog_exhausted"
CAMPAIGN_COMPLETED = "completed"

# A dispatcher runs one worker session for one idea and reports its trial's meta; an idea
# generator proposes one more idea (its meta, sans ``origin``/``model_id`` which this
# module stamps) when the backlog holds nothing more to try.
Dispatcher = Callable[[RegistrySpace, Fact, Fact], dict[str, object]]
IdeaGenerator = Callable[[RegistrySpace, str, Optional[str], frozenset], Optional[dict[str, object]]]


@dataclass(frozen=True)
class Intervention:
    """One caller-supplied constraint on this dispatch's candidate draw.

    ``kind="forced_axis"`` pins the draw to ``axis`` regardless of seed-first order.
    ``kind="exclude_axis"`` removes ``axis`` from the permitted set UNLESS doing so would
    leave no untried idea anywhere else, in which case it is recorded unsatisfiable
    instead of being applied.
    """

    kind: str
    axis: str

    def __post_init__(self) -> None:
        if self.kind not in INTERVENTION_KINDS:
            raise RegistryValidationError(
                f"intervention kind must be one of {INTERVENTION_KINDS}, got {self.kind!r}", field="kind"
            )


def _model(space: RegistrySpace, model_id: str) -> Fact:
    model = space.get(model_id)
    if model is None or model.category != MODEL:
        raise RegistryValidationError(f"model {model_id!r} was never registered", field="model_id")
    return model


def non_voided_trial_count(space: RegistrySpace, model_id: str) -> int:
    """Trials counted against ``max_trials`` -- a voided trial is excluded."""
    return sum(
        1
        for t in space.list_facts(TRIAL)
        if t.meta.get("model_id") == model_id and t.meta.get("status") != TRIAL_STATUS_VOIDED
    )


def resolve_interventions(
    space: RegistrySpace, model_id: str, interventions: tuple[Intervention, ...]
) -> tuple[Optional[str], frozenset, list[Intervention]]:
    """``(forced_axis, permitted_axes, unsatisfiable)`` for one dispatch, recomputed from
    the CURRENT untried backlog -- never from a prior dispatch's result.

    ``permitted_axes`` is every axis carried by ``model_id``'s untried backlog, minus any
    ``exclude_axis`` intervention whose exclusion still leaves at least one untried idea
    standing. An exclusion that would leave nothing is UNSATISFIABLE: it is returned in
    ``unsatisfiable`` and never removes its axis from ``permitted_axes``.
    """
    untried = untried_backlog(space, model_id=model_id)
    all_axes = frozenset(str(idea.meta.get("axis")) for idea in untried)

    forced_axis: Optional[str] = None
    excluded: set[str] = set()
    unsatisfiable: list[Intervention] = []
    for iv in interventions:
        if iv.kind == FORCED_AXIS:
            if forced_axis is None:
                forced_axis = iv.axis
        elif iv.kind == EXCLUDE_AXIS:
            remaining = [idea for idea in untried if str(idea.meta.get("axis")) != iv.axis]
            if remaining:
                excluded.add(iv.axis)
            else:
                unsatisfiable.append(iv)

    permitted_axes = all_axes - excluded
    return forced_axis, permitted_axes, unsatisfiable


def _select_candidate(
    space: RegistrySpace, model_id: str, forced_axis: Optional[str], permitted_axes: frozenset
) -> Optional[Fact]:
    untried = untried_backlog(space, model_id=model_id)
    if forced_axis is not None:
        pool = [idea for idea in untried if str(idea.meta.get("axis")) == forced_axis]
    else:
        pool = [idea for idea in untried if str(idea.meta.get("axis")) in permitted_axes]

    for origin in (SEEDED, DISCOVERED):
        for idea in pool:
            if idea.meta.get("origin") == origin:
                return idea
    return None


def dispatch_trial(
    space: RegistrySpace,
    model_id: str,
    ledger_rows: dict[str, LedgerRow],
    dispatcher: Dispatcher,
    *,
    interventions: tuple[Intervention, ...] = (),
    idea_generator: Optional[IdeaGenerator] = None,
) -> dict[str, object]:
    """Run ONE worker session: pick a candidate idea, dispatch it, register the trial, and
    hand it to :func:`~knowledge.ml_registry.verdict.adjudicate_verdict` for the full
    adopt/park/reject/void adjudication -- adjudicate_verdict itself applies every side
    effect (idea adoption/park/reject, the ratchet counter, and any streak-triggered
    baseline invalidation) before this returns.

    Returns a dict describing what happened; ``result["candidate"] is None`` means the
    backlog (seeded, discovered, and generator-proposable) is exhausted for this dispatch
    -- :func:`supervise_campaign` treats that as a close condition, not an error.
    """
    model = _model(space, model_id)
    forced_axis, permitted_axes, unsatisfiable = resolve_interventions(space, model_id, interventions)
    result: dict[str, object] = {"unsatisfiable_interventions": list(unsatisfiable), "forced_axis": forced_axis}

    idea = _select_candidate(space, model_id, forced_axis, permitted_axes)
    origin_used = idea.meta.get("origin") if idea is not None else None

    if idea is None and idea_generator is not None:
        proposed = idea_generator(space, model_id, forced_axis, permitted_axes)
        if proposed is not None:
            meta = dict(proposed)
            meta["model_id"] = model_id
            meta["origin"] = DISCOVERED
            meta.setdefault("axis", forced_axis or "discovered")
            idea_id = register_idea(space, meta)  # registered -- with axis, basis, origin -- before its trial
            idea = space.get(idea_id)
            origin_used = DISCOVERED

    if idea is None:
        result["candidate"] = None
        return result

    trial_meta = dict(dispatcher(space, model, idea))
    trial_meta.setdefault("model_id", model_id)
    trial_meta.setdefault("idea_id", idea.id)
    trial_meta.setdefault("status", "running")  # adjudicate_verdict below sets the real status
    row = ledger_rows.get(str(trial_meta.get("commit")))
    if row is not None:
        # self-reported throughput/diff_lines agree with the ledger by construction
        # unless the dispatcher explicitly overrides them (e.g. to exercise a refusal).
        trial_meta.setdefault("throughput", row.throughput)
        trial_meta.setdefault("diff_lines", row.diff_lines)
    ledger_commits = frozenset(ledger_rows.keys())
    trial_id = register_trial(space, trial_meta, ledger_commits)
    trial = space.get(trial_id)
    assert trial is not None

    result.update(candidate=idea.id, origin=origin_used, trial_id=trial_id)

    if trial.meta.get("status") == TRIAL_STATUS_VOIDED:
        result["status"] = TRIAL_STATUS_VOIDED
        return result

    result["status"] = adjudicate_verdict(space, trial_id, ledger_rows)
    return result


def _record_close(space: RegistrySpace, model_id: str, close: str) -> None:
    model = _model(space, model_id)
    model.meta[CAMPAIGN_STATUS_FIELD] = CLOSE_WON if close == CLOSE_WON else CAMPAIGN_COMPLETED


def supervise_campaign(
    space: RegistrySpace,
    model_id: str,
    ledger_rows: dict[str, LedgerRow],
    dispatcher: Dispatcher,
    *,
    interventions: tuple[Intervention, ...] = (),
    idea_generator: Optional[IdeaGenerator] = None,
    max_dispatches: Optional[int] = None,
) -> dict[str, object]:
    """Drive ``model_id``'s campaign to close, dispatching one worker per trial serially.

    Each iteration calls :func:`dispatch_trial` exactly once -- a fresh, independent
    worker session that ends with its trial -- and evaluates the close condition only
    AFTER that trial's :func:`~knowledge.ml_registry.verdict.adjudicate_verdict` side
    effects have landed on ``space``. There is no wall-clock or round-boundary timeout
    here: the loop runs for as many dispatches as the campaign needs (or until
    ``max_dispatches``, a TEST-ONLY cap -- a real caller omits it and simply calls this
    again on resume). A voided trial never counts against ``max_trials`` and never itself
    closes the campaign.

    Close conditions, checked in this order once a dispatch's side effects have landed:
      1. the dispatch found no candidate -> :data:`CLOSE_BACKLOG_EXHAUSTED`.
      2. the dispatched trial was adopted (the win condition) -> :data:`CLOSE_WON`.
      3. the non-voided trial count has reached ``max_trials`` -> :data:`CLOSE_MAX_TRIALS`.
    Every non-win close is recorded on the model as a completed outcome
    (:data:`CAMPAIGN_COMPLETED`); a win is recorded as :data:`CLOSE_WON`.
    """
    model = _model(space, model_id)
    max_trials = int(model.meta.get("max_trials", MODEL_DEFAULTS["max_trials"]))

    history: list[dict[str, object]] = []
    dispatches = 0
    while max_dispatches is None or dispatches < max_dispatches:
        result = dispatch_trial(
            space, model_id, ledger_rows, dispatcher,
            interventions=interventions, idea_generator=idea_generator,
        )
        dispatches += 1
        history.append(result)

        if result["candidate"] is None:
            close = CLOSE_BACKLOG_EXHAUSTED
            _record_close(space, model_id, close)
            return {"history": history, "close": close}

        if result["status"] == TRIAL_STATUS_VOIDED:
            continue  # does not count against max_trials; loop continues

        if result["status"] == VERDICT_ADOPTED:
            close = CLOSE_WON
            _record_close(space, model_id, close)
            return {"history": history, "close": close}

        if non_voided_trial_count(space, model_id) >= max_trials:
            close = CLOSE_MAX_TRIALS
            _record_close(space, model_id, close)
            return {"history": history, "close": close}

    return {"history": history, "close": None}
