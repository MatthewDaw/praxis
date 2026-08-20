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

(R9) AXIS WATCHDOG -- two more interventions, computed fresh each dispatch from the trial
history alone (never cached), and merged onto whatever the caller supplies before
:func:`resolve_interventions` runs (a caller-supplied intervention always takes
precedence, matching R8's forced-axis-over-seed-first rule -- an auto-fired forced axis is
never drawn from an axis the CALLER excluded):

* the "rabbit-hole" intervention -- :data:`NON_IMPROVING_STREAK_TRIGGER` (2) trailing
  non-improving (not adopted) trials in a row on the SAME axis auto-fires an
  ``exclude_axis`` intervention against it. A model carrying its own
  ``non_improving_streak_trigger`` meta field raises (or lowers) that bar for itself alone
  -- see :func:`model_non_improving_trigger`; a model that sets nothing keeps the 2. The
  intervention fires UNLESS a durable :func:`record_keep_pushing_marker`
  is on file for that axis. That marker is the ONLY suppression: nothing a dispatcher
  returns in a trial's own payload can suppress it, only an explicitly-authored, separately
  recorded marker can. An out-of-diff code change (a change landed outside any trial's own
  diff, e.g. a manual harness edit) resets the trailing non-improving count to zero -- but
  it too is authored OUT OF BAND, via :func:`record_out_of_diff_change`, exactly like the
  keep-pushing marker: ``dispatch_trial`` STRIPS any ``out_of_diff_change`` key a dispatcher
  puts in its own trial payload, so the judged worker can never renew its own reset budget.
  Only the FIRST recorded change within the current same-axis run resets the count; a second
  one on the same run does not reset it again.
* the "axis-coverage" intervention -- :data:`SAME_AXIS_STREAK_TRIGGER` (5) trailing trials
  in a row on the same axis, REGARDLESS of verdict or any keep-pushing marker, force the
  next draw onto a retrieval axis (:data:`~knowledge.ml_registry.ideate.RETRIEVAL_AXES`)
  with untried backlog -- an escape valve a keep-pushing marker cannot block, so a marker
  can extend a stuck axis's run but never trap the campaign on it forever. When no such
  retrieval axis is available the watchdog FALLS THROUGH to the rabbit-hole check rather
  than returning empty-handed: being more stuck must never disable the weaker guard.

(R17) An optional ``lesson_filer`` threaded through :func:`dispatch_trial`/
:func:`supervise_campaign` offers each terminal-status idea (and, at close, a final sweep of
them) to :mod:`knowledge.ml_registry.insight`'s cross-model confirmation gate -- an af-learn
lesson is filed only for an insight the registry itself shows recurring across >= 2 distinct
models, never at trial granularity, and never more than once per confirmed insight.

TERMINATION. Every campaign this module drives is bounded without relying on
``max_dispatches``: ``max_trials`` bounds adjudicated trials, :data:`DEFAULT_MAX_CONSECUTIVE_VOIDS`
bounds ledger-voided ones (a void is decided ONLY by
:func:`~knowledge.ml_registry.verdict.adjudicate_verdict`'s ledger-throughput check -- never
by a ``status`` a dispatcher reports about itself, which :func:`dispatch_trial` overwrites),
an exhausted discovered-idea budget CLOSES the campaign rather than raising out of it, and a
dispatcher exceeding the model's ``per_trial_seconds`` closes it too.
"""

from __future__ import annotations

import time
import warnings
from dataclasses import dataclass
from typing import Callable, Optional

from knowledge.ml_registry.floor import CAMPAIGN_STATUS_FIELD
from knowledge.ml_registry.ideate import RETRIEVAL_AXES
from knowledge.ml_registry.insight import LessonFiler, maybe_file_cross_model_lesson, sweep_cross_model_lessons
from knowledge.ml_registry.lifecycle import TRIAL_STATUS_SUCCEEDED, claim_is_stale, untried_backlog
from knowledge.ml_registry.schema import (
    IDEA,
    MODEL,
    TERMINAL_TRIAL_STATUSES,
    TRIAL,
    TRIAL_STATUS_VOIDED,
    RegistryValidationError,
)
from knowledge.ml_registry.verdict import LedgerRow, VERDICT_ADOPTED, adjudicate_verdict
from knowledge.ml_registry.write_path import (
    DISCOVERED,
    MAX_DISCOVERED_IDEAS_FIELD,
    MODEL_DEFAULTS,
    SEEDED,
    UNLIMITED_DISCOVERED_IDEAS,
    Fact,
    RegistrySpace,
    discovered_idea_budget,
    register_idea,
    register_trial,
    supersede_trial,
)

FORCED_AXIS = "forced_axis"
EXCLUDE_AXIS = "exclude_axis"
INTERVENTION_KINDS: tuple[str, ...] = (FORCED_AXIS, EXCLUDE_AXIS)

# (R9) axis-watchdog thresholds -- see the module docstring's AXIS WATCHDOG section.
NON_IMPROVING_STREAK_TRIGGER = 2
NON_IMPROVING_STREAK_TRIGGER_FIELD = "non_improving_streak_trigger"
SAME_AXIS_STREAK_TRIGGER = 5

# The durable keep-pushing marker lives on the model fact, keyed by axis: {axis: {"author": str}}.
# Recorded ONLY via record_keep_pushing_marker -- never derivable from a dispatcher's trial payload.
KEEP_PUSHING_MARKER_FIELD = "keep_pushing_markers"

# Durable out-of-diff-change marks, likewise on the model fact and likewise authored out of
# band (record_out_of_diff_change). Each entry is {"before_trial_index": int, "author": str}:
# the index, into the model's non-voided trial sequence, of the first trial that FOLLOWS the
# out-of-band change. A dispatcher's own payload is never consulted -- see dispatch_trial.
OUT_OF_DIFF_CHANGE_FIELD = "out_of_diff_changes"

# Keys a dispatcher (the JUDGED party) may never set on its own trial: its status is decided
# by adjudicate_verdict against the ledger, and an out-of-diff change is authored out of band.
DISPATCHER_FORBIDDEN_TRIAL_KEYS: tuple[str, ...] = ("status", "out_of_diff_change")

TRIAL_STATUS_RUNNING = "running"
TRIAL_STATUS_TIMED_OUT = "timed_out"

# A campaign whose dispatches keep coming back ledger-voided is burning compute on an
# unreliable harness; this many CONSECUTIVE voids close it. Recomputed from the registry's
# trial history every time (never held in loop state); overridable per model via the
# model fact's "max_consecutive_voids".
DEFAULT_MAX_CONSECUTIVE_VOIDS = 3
MAX_CONSECUTIVE_VOIDS_FIELD = "max_consecutive_voids"

CLOSE_WON = "won"
CLOSE_MAX_TRIALS = "max_trials_reached"
CLOSE_BACKLOG_EXHAUSTED = "backlog_exhausted"
CLOSE_VOID_LIMIT = "void_limit_reached"
CLOSE_TRIAL_TIMEOUT = "per_trial_seconds_exceeded"
CAMPAIGN_COMPLETED = "completed"

# --- win conditions (structured) ---------------------------------------------------------
# A model's declared win_condition must be EVALUABLE, or its campaign is refused rather than
# silently closing "won" on the first adoption. Three accepted forms, see parse_win_condition.
WIN_METRIC_AT_MOST = "metric_at_most"
WIN_METRIC_AT_LEAST = "metric_at_least"
WIN_THRESHOLD_KINDS: tuple[str, ...] = (WIN_METRIC_AT_MOST, WIN_METRIC_AT_LEAST)
WIN_ON_ADOPTION = "beats baseline by noise_floor"

#: A model's explicit, per-campaign opt-in to first-adoption-wins. Only bootstrap ever
#: validated a win condition -- build_model_meta refuses the bare string, the
#: win_condition_declared precondition reports it, and `bootstrap-campaign --win-condition`
#: is a required flag -- while register_model, register_model_with_baseline and their two CLI
#: verbs accept it unexamined (schema.py only asks that the key be non-empty). So a campaign
#: stood up WITHOUT bootstrap was one string away from closing WON on trial one, and it was
#: reproduced end to end: register-model with that string succeeded and supervise-campaign
#: closed the campaign after a SINGLE dispatch on a +1.2-floor adoption, leaving every other
#: declared stage untried. The string stays supported -- test fixtures and
#: sports_analysis's experiments/ball_campaign genuinely mean "the adoption IS the win" --
#: but meaning it now has to be SAID, on the model, next to the string.
WIN_ON_ADOPTION_OPT_IN_FIELD = "win_on_adoption_ok"

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


def consecutive_void_count(space: RegistrySpace, model_id: str) -> int:
    """How many of ``model_id``'s MOST RECENT trials in a row were ledger-voided.

    Recomputed from the registry's own trial facts on every call -- a resumed campaign sees
    the same count a continuous one would, because nothing about it lives in loop state.
    """
    trials = [t for t in space.list_facts(TRIAL) if t.meta.get("model_id") == model_id]
    voids = 0
    for trial in reversed(trials):
        if trial.meta.get("status") != TRIAL_STATUS_VOIDED:
            break
        voids += 1
    return voids


def parse_win_condition(win_condition: object) -> dict[str, object]:
    """Parse a model's declared ``win_condition`` into an evaluable predicate, or refuse it.

    Accepted forms:

    * ``{"metric_at_most": x}`` / ``{"metric_at_least": x}`` -- the campaign is won once an
      ADOPTED trial's LEDGER value reaches the threshold ``x``.
    * the string ``"metric_at_most: 0.80"`` (or ``"metric_at_least 0.80"``) -- the same, in
      the string form a registration or CLI flag can carry.
    * the string :data:`WIN_ON_ADOPTION` (``"beats baseline by noise_floor"``) -- the model
      declares that beating the baseline by more than one noise floor IS the win, i.e. the
      adjudicated adoption itself. This is a DECLARED condition, not a fallback.

    Anything else is refused naming ``win_condition``: an unevaluable win condition must
    never be silently downgraded to "the first adoption wins".
    """
    if isinstance(win_condition, dict):
        keys = [k for k in WIN_THRESHOLD_KINDS if k in win_condition]
        if len(keys) == 1 and len(win_condition) == 1:
            try:
                return {"kind": keys[0], "threshold": float(win_condition[keys[0]])}  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass
    elif isinstance(win_condition, str):
        text = win_condition.strip()
        if text.lower() == WIN_ON_ADOPTION:
            return {"kind": WIN_ON_ADOPTION}
        for kind in WIN_THRESHOLD_KINDS:
            if text.lower().startswith(kind):
                remainder = text[len(kind):].lstrip(": ").strip()
                try:
                    return {"kind": kind, "threshold": float(remainder)}
                except ValueError:
                    break
    raise RegistryValidationError(
        f"win_condition {win_condition!r} is not evaluable; declare {WIN_THRESHOLD_KINDS} "
        f"(e.g. {{'metric_at_most': 0.80}}) or {WIN_ON_ADOPTION!r}",
        field="win_condition",
    )


def check_win_on_adoption_declared(model: Fact) -> None:
    """Refuse a model whose win condition is the bare :data:`WIN_ON_ADOPTION` string unless it
    carries :data:`WIN_ON_ADOPTION_OPT_IN_FIELD`.

    The string itself is not the defect -- :func:`parse_win_condition` still accepts it, and
    a campaign that really does mean "one adoption beyond the noise floor IS the win" is
    entitled to say so. The defect was the COVERAGE GAP around it: bootstrap validated win
    conditions and nothing else did, so the four surfaces that stand a campaign up outside
    bootstrap (``register_model``, ``register_model_with_baseline`` and both of their CLI
    verbs) took the string unexamined, and a campaign whose author simply never thought about
    winning closed WON on its first adopted trial with every other stage untried.

    Raises rather than returns a flag so a PREFLIGHT can gate on it before a campaign starts
    -- which is where the answer still costs one line. ``af-ml-campaign-loop.sh`` calls it
    that way and refuses to launch; :func:`supervise_campaign` only WARNS on it, see there.
    """
    condition = parse_win_condition(model.meta.get("win_condition"))
    if condition["kind"] != WIN_ON_ADOPTION or model.meta.get(WIN_ON_ADOPTION_OPT_IN_FIELD) is True:
        return
    raise RegistryValidationError(
        f"win_condition {WIN_ON_ADOPTION!r} makes the FIRST adopted trial close this campaign "
        f"as WON, leaving every other declared stage untried. Declare a numeric target "
        f"({WIN_THRESHOLD_KINDS}, e.g. {{'metric_at_least': 0.90}}), or, if first-adoption-wins "
        f"is genuinely what this campaign means, say so on the model: "
        f"{WIN_ON_ADOPTION_OPT_IN_FIELD}=true",
        field="win_condition",
    )


def win_condition_met(
    space: RegistrySpace, model_id: str, trial_id: str, ledger_rows: dict[str, LedgerRow]
) -> bool:
    """Whether ``model_id``'s DECLARED win condition is satisfied by ``trial_id``'s LEDGER row.

    Called only for a trial adjudicate_verdict already ADOPTED. A threshold condition is
    checked against the external ledger's value for the trial's commit -- never against
    anything the dispatcher reported -- so the first 0.001 improvement no longer closes a
    campaign whose declared target is, say, ``val_bpb <= 0.80``.
    """
    model = _model(space, model_id)
    condition = parse_win_condition(model.meta.get("win_condition"))
    if condition["kind"] == WIN_ON_ADOPTION:
        return True

    trial = space.get(trial_id)
    if trial is None:
        raise RegistryValidationError(f"trial {trial_id!r} was never registered", field="trial_id")
    commit = str(trial.meta.get("commit"))
    row = ledger_rows.get(commit)
    if row is None:
        raise RegistryValidationError(
            f"commit {commit!r} has no matching row in the external ledger", field="commit"
        )
    threshold = float(condition["threshold"])  # type: ignore[arg-type]
    return row.value <= threshold if condition["kind"] == WIN_METRIC_AT_MOST else row.value >= threshold


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


def _discovered_budget_remains(space: RegistrySpace, model_id: str) -> bool:
    """Whether ``model_id`` may still register another ``origin="discovered"`` idea.

    Recomputed from the registry each dispatch. A model at its ``max_discovered_ideas``
    budget simply has no generator-proposable candidate left -- the campaign CLOSES on
    backlog exhaustion rather than the budget refusal escaping as an exception.
    """
    model = _model(space, model_id)
    budget = discovered_idea_budget(model.meta)
    if budget == UNLIMITED_DISCOVERED_IDEAS:
        return True
    already = sum(
        1
        for f in space.list_facts(IDEA)
        if f.meta.get("model_id") == model_id and f.meta.get("origin") == DISCOVERED
    )
    return already < budget


def record_keep_pushing_marker(space: RegistrySpace, model_id: str, axis: str, author: str) -> None:
    """Durably record a keep-pushing marker for ``axis`` on ``model_id``, carrying ``author``.

    This is the ONLY way to suppress the rabbit-hole (axis-switch) intervention for that
    axis. A dispatcher's trial payload is never consulted for suppression -- only an
    explicit call here, with a non-empty author, has any effect.
    """
    if not str(author or "").strip():
        raise RegistryValidationError("a keep-pushing marker must carry a non-empty author", field="author")
    model = _model(space, model_id)
    markers = dict(model.meta.get(KEEP_PUSHING_MARKER_FIELD) or {})
    markers[axis] = {"author": str(author)}
    model.meta[KEEP_PUSHING_MARKER_FIELD] = markers


def _model_trials(space: RegistrySpace, model_id: str) -> list[Fact]:
    """``model_id``'s NON-VOIDED trials in registration order -- the sequence the axis
    watchdog and the out-of-diff marks both index into."""
    return [
        t for t in space.list_facts(TRIAL)
        if t.meta.get("model_id") == model_id and t.meta.get("status") != TRIAL_STATUS_VOIDED
    ]


def record_out_of_diff_change(space: RegistrySpace, model_id: str, author: str) -> None:
    """Durably record that a code change landed OUTSIDE any trial's own diff (a manual
    harness edit, say), carrying ``author``.

    This is the ONLY way an out-of-diff change resets the rabbit-hole watchdog's trailing
    non-improving count. Like :func:`record_keep_pushing_marker` it is authored out of band:
    a dispatcher cannot claim one in its trial payload (``dispatch_trial`` strips the key),
    so the judged worker can never renew its own reset budget by alternating axes.

    The mark is anchored to the model's CURRENT non-voided trial count, i.e. it applies to
    the next trial registered after this call.
    """
    if not str(author or "").strip():
        raise RegistryValidationError("an out-of-diff-change mark must carry a non-empty author", field="author")
    model = _model(space, model_id)
    marks = list(model.meta.get(OUT_OF_DIFF_CHANGE_FIELD) or [])
    marks.append({"before_trial_index": len(_model_trials(space, model_id)), "author": str(author)})
    model.meta[OUT_OF_DIFF_CHANGE_FIELD] = marks


def _out_of_diff_indices(space: RegistrySpace, model_id: str) -> frozenset[int]:
    model = _model(space, model_id)
    marks = model.meta.get(OUT_OF_DIFF_CHANGE_FIELD) or []
    if not isinstance(marks, list):
        return frozenset()
    return frozenset(
        int(m["before_trial_index"])
        for m in marks
        if isinstance(m, dict) and m.get("before_trial_index") is not None
    )


def _trial_axis(space: RegistrySpace, trial: Fact) -> Optional[str]:
    idea = space.get(trial.derived_from[0]) if trial.derived_from else None
    return str(idea.meta.get("axis")) if idea is not None else None


def axis_streak(space: RegistrySpace, model_id: str) -> dict[str, object]:
    """Recomputed fresh from ``model_id``'s trial history (registration order), never
    carried across calls.

    Returns ``{"axis": ..., "same_axis_streak": ..., "non_improving_streak": ...}`` for the
    TRAILING run of non-voided trials sharing the most recent trial's axis: how many trials
    long that run is, and how many of its trailing trials were non-improving (not adopted),
    honoring the single out-of-diff-change reset described in the module docstring. That
    reset is read from the model's DURABLE, out-of-band :func:`record_out_of_diff_change`
    marks -- never from a trial payload the judged dispatcher authored. ``axis`` is ``None``
    when the model has no non-voided trials yet.
    """
    trials = _model_trials(space, model_id)
    if not trials:
        return {"axis": None, "same_axis_streak": 0, "non_improving_streak": 0}

    last_axis = _trial_axis(space, trials[-1])
    same_axis_run: list[Fact] = []
    for t in reversed(trials):
        if _trial_axis(space, t) != last_axis:
            break
        same_axis_run.append(t)
    same_axis_run.reverse()  # chronological order within the trailing run

    out_of_diff_at = _out_of_diff_indices(space, model_id)
    run_offset = len(trials) - len(same_axis_run)
    non_improving_streak = 0
    reset_used = False
    for offset, t in enumerate(same_axis_run):
        if (run_offset + offset) in out_of_diff_at and not reset_used:
            non_improving_streak = 0
            reset_used = True
        if t.meta.get("status") == TRIAL_STATUS_SUCCEEDED:
            non_improving_streak = 0
        else:
            non_improving_streak += 1

    return {"axis": last_axis, "same_axis_streak": len(same_axis_run), "non_improving_streak": non_improving_streak}


def _next_retrieval_axis(
    space: RegistrySpace, model_id: str, blocked: frozenset = frozenset()
) -> Optional[str]:
    axes_present = {str(idea.meta.get("axis")) for idea in untried_backlog(space, model_id=model_id)}
    for axis in RETRIEVAL_AXES:
        if axis in axes_present and axis not in blocked:
            return axis
    return None


def model_non_improving_trigger(model_meta: dict[str, object]) -> int:
    """How many trailing non-improving trials on one axis auto-fire the rabbit-hole
    ``exclude_axis`` intervention for THIS model.

    A model may raise (or lower) the bar for itself by putting
    ``non_improving_streak_trigger`` on its own meta -- a wide stage, say a ten-arm
    representation sweep, is otherwise abandoned after its second miss with eight arms
    never dispatched. A model that sets nothing, or sets an unusable value (non-integer,
    or below 1), falls back to :data:`NON_IMPROVING_STREAK_TRIGGER`, so every campaign
    that never opts in behaves exactly as before.
    """
    raw = model_meta.get(NON_IMPROVING_STREAK_TRIGGER_FIELD)
    if raw is None or isinstance(raw, bool):
        return NON_IMPROVING_STREAK_TRIGGER
    try:
        trigger = int(raw)
    except (TypeError, ValueError):
        return NON_IMPROVING_STREAK_TRIGGER
    return trigger if trigger >= 1 else NON_IMPROVING_STREAK_TRIGGER


def _auto_interventions(
    space: RegistrySpace, model_id: str, caller_interventions: tuple[Intervention, ...] = ()
) -> tuple[Intervention, ...]:
    """The axis-watchdog's own auto-fired interventions for this dispatch -- see the module
    docstring's AXIS WATCHDOG section.

    A CALLER-supplied intervention always takes precedence: a caller-supplied forced axis
    wins in :func:`resolve_interventions` (it is merged ahead of these), and an axis the
    caller EXCLUDED is never handed back as an auto-fired forced axis here -- the
    axis-coverage escape valve reaches for the next retrieval axis the caller left open, or,
    finding none, falls through to the rabbit-hole check below.
    """
    streak = axis_streak(space, model_id)
    axis = streak["axis"]
    if axis is None:
        return ()

    caller_excluded = frozenset(iv.axis for iv in caller_interventions if iv.kind == EXCLUDE_AXIS)

    if streak["same_axis_streak"] >= SAME_AXIS_STREAK_TRIGGER:
        retrieval_axis = _next_retrieval_axis(space, model_id, blocked=caller_excluded)
        if retrieval_axis is not None:
            return (Intervention(kind=FORCED_AXIS, axis=retrieval_axis),)
        # No retrieval axis available -- FALL THROUGH to the rabbit-hole check rather than
        # returning empty: being MORE stuck must never disable the weaker guard.

    model = _model(space, model_id)
    if streak["non_improving_streak"] >= model_non_improving_trigger(model.meta):
        markers = model.meta.get(KEEP_PUSHING_MARKER_FIELD) or {}
        marker = markers.get(axis) if isinstance(markers, dict) else None
        suppressed = isinstance(marker, dict) and bool(str(marker.get("author") or "").strip())
        if not suppressed:
            return (Intervention(kind=EXCLUDE_AXIS, axis=axis),)

    return ()


def _reclaim_dead_trial(
    space: RegistrySpace,
    idea: Fact,
    trial_meta: dict[str, object],
    ledger_commits: frozenset[str],
    refusal: RegistryValidationError,
) -> str:
    """Supersede the in-flight trial of an idea whose CLAIM LEASE HAS EXPIRED, then register
    the new one. Re-raises the refusal untouched for any other cause.

    :func:`~knowledge.ml_registry.write_path.register_trial` allows an idea only one trial in
    flight, and that trial has no lease of its own -- so a worker killed mid-run leaves it
    ``running`` forever. Meanwhile the IDEA's claim does have a lease
    (:data:`~knowledge.ml_registry.lifecycle.DEFAULT_IDEA_CLAIM_LEASE_TTL_S`, 900s), it goes
    stale, and :func:`~knowledge.ml_registry.lifecycle.untried_backlog` hands the idea back
    to :func:`_select_candidate` -- which picks it, is refused, and the refusal propagated
    uncaught: every subsequent supervise-campaign died on the same idea, with a manual
    ``supersede-trial`` the only way out.

    The escape is the LEASE, not a timeout invented here, so this cannot cancel a run that
    might still be alive: a live-leased claim means a worker heartbeating within the last
    900s (that idea is not even in the backlog, so this path is unreachable for it), and an
    idea with NO claim at all is no evidence of a dead worker either -- both re-raise, and
    a genuinely-running trial keeps the one-in-flight protection the rule exists for. Only
    an EXPIRED lease -- a worker that stopped checking in -- is treated as dead, which is the
    same inference :func:`untried_backlog` already makes when it re-offers the idea.
    """
    if not claim_is_stale(idea):
        raise refusal
    in_flight = [
        f for f in space.list_facts(TRIAL)
        if str(f.meta.get("idea_id")) == idea.id
        and str(f.meta.get("status", "")) not in TERMINAL_TRIAL_STATUSES
    ]
    if not in_flight:
        raise refusal
    for trial in in_flight:
        supersede_trial(
            space,
            trial.id,
            f"idea claim lease went stale (owner {idea.meta.get('claim_owner')!r} stopped "
            f"heartbeating); the worker running this trial is dead, not slow",
        )
    return register_trial(space, trial_meta, ledger_commits)


def dispatch_trial(
    space: RegistrySpace,
    model_id: str,
    ledger_rows: dict[str, LedgerRow],
    dispatcher: Dispatcher,
    *,
    interventions: tuple[Intervention, ...] = (),
    idea_generator: Optional[IdeaGenerator] = None,
    lesson_filer: Optional[LessonFiler] = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Run ONE worker session: pick a candidate idea, dispatch it, register the trial, and
    hand it to :func:`~knowledge.ml_registry.verdict.adjudicate_verdict` for the full
    adopt/park/reject/void adjudication -- adjudicate_verdict itself applies every side
    effect (idea adoption/park/reject, the ratchet counter, and any streak-triggered
    baseline invalidation) before this returns.

    Returns a dict describing what happened; ``result["candidate"] is None`` means the
    backlog (seeded, discovered, and generator-proposable) is exhausted for this dispatch
    -- :func:`supervise_campaign` treats that as a close condition, not an error. An
    ``idea_generator`` whose proposal would exceed the model's ``max_discovered_ideas``
    budget is one such exhaustion: the budget refusal CLOSES the campaign instead of
    raising out of it.

    STATUS IS NOT SELF-REPORTED. Whatever the dispatcher returns is its evidence, not its
    verdict: every key in :data:`DISPATCHER_FORBIDDEN_TRIAL_KEYS` is STRIPPED from its
    payload before the trial is registered. In particular a dispatcher cannot report itself
    ``"voided"`` -- a void is decided solely by
    :func:`~knowledge.ml_registry.verdict.adjudicate_verdict`'s ledger-throughput check,
    which also marks the trial, so the void counts toward the campaign's void bound and the
    idea's redraw can never loop forever.

    The dispatcher call is TIMED against the model's ``per_trial_seconds`` budget (injected
    ``clock`` for tests). A dispatcher that overruns it yields
    ``status == "timed_out"`` and NO registered trial;
    :func:`supervise_campaign` closes the campaign on it.

    (R17) When the dispatched trial's adjudication lands the idea in a TERMINAL status
    (adopted/rejected/parked), this checks -- via
    :func:`~knowledge.ml_registry.insight.maybe_file_cross_model_lesson` -- whether the
    idea's insight is now a CONFIRMED cross-model pattern (>= 2 distinct models), and, only
    if so and only if ``lesson_filer`` is supplied, files exactly one af-learn lesson for it.
    A voided trial never reaches this: its idea stays untried, so nothing here ever fires at
    trial granularity alone.

    A candidate whose previous worker DIED mid-trial -- its idea claim lease expired, which
    is why the idea came back to the backlog at all -- has that dead trial superseded and
    replaced rather than raising register_trial's one-in-flight refusal out of the campaign
    forever (:func:`_reclaim_dead_trial`, ``result["superseded_dead_trial"]``). A trial whose
    worker is merely SLOW is never touched: its claim lease is live.
    """
    model = _model(space, model_id)
    all_interventions = interventions + _auto_interventions(space, model_id, interventions)
    forced_axis, permitted_axes, unsatisfiable = resolve_interventions(space, model_id, all_interventions)
    result: dict[str, object] = {"unsatisfiable_interventions": list(unsatisfiable), "forced_axis": forced_axis}

    idea = _select_candidate(space, model_id, forced_axis, permitted_axes)
    origin_used = idea.meta.get("origin") if idea is not None else None

    if idea is None and idea_generator is not None and _discovered_budget_remains(space, model_id):
        proposed = idea_generator(space, model_id, forced_axis, permitted_axes)
        if proposed is not None:
            meta = dict(proposed)
            meta["model_id"] = model_id
            meta["origin"] = DISCOVERED
            meta.setdefault("axis", forced_axis or "discovered")
            try:
                # registered -- with axis, basis, origin -- before its trial
                idea_id = register_idea(space, meta)
            except RegistryValidationError as exc:
                # An exhausted discovered-idea budget CLOSES the campaign (no candidate); it
                # is a cost control firing, not a crash. Any other refusal still propagates.
                if exc.field != MAX_DISCOVERED_IDEAS_FIELD:
                    raise
                result["discovered_budget_exhausted"] = True
            else:
                idea = space.get(idea_id)
                origin_used = DISCOVERED

    if idea is None:
        result["candidate"] = None
        return result

    per_trial_seconds = float(model.meta.get("per_trial_seconds", MODEL_DEFAULTS["per_trial_seconds"]))
    started_at = clock()
    payload = dispatcher(space, model, idea)
    elapsed = clock() - started_at
    result.update(candidate=idea.id, origin=origin_used, elapsed_seconds=elapsed)
    if elapsed > per_trial_seconds:
        # The per-trial cost budget is a real bound, not documentation: the overrun run is
        # not registered as a trial and supervise_campaign closes the campaign on it.
        result.update(trial_id=None, status=TRIAL_STATUS_TIMED_OUT, per_trial_seconds=per_trial_seconds)
        return result

    trial_meta = {k: v for k, v in payload.items() if k not in DISPATCHER_FORBIDDEN_TRIAL_KEYS}
    trial_meta.setdefault("model_id", model_id)
    trial_meta.setdefault("idea_id", idea.id)
    # OVERWRITTEN, never setdefault: the judged dispatcher does not get to declare its own
    # trial's status (and so cannot self-report a void). adjudicate_verdict sets the real one.
    trial_meta["status"] = TRIAL_STATUS_RUNNING
    row = ledger_rows.get(str(trial_meta.get("commit")))
    if row is not None:
        # self-reported throughput/diff_lines agree with the ledger by construction
        # unless the dispatcher explicitly overrides them (e.g. to exercise a refusal).
        trial_meta.setdefault("throughput", row.throughput)
        trial_meta.setdefault("diff_lines", row.diff_lines)
    ledger_commits = frozenset(ledger_rows.keys())
    try:
        trial_id = register_trial(space, trial_meta, ledger_commits)
    except RegistryValidationError as exc:
        if exc.field != "idea_id":
            raise
        trial_id = _reclaim_dead_trial(space, idea, trial_meta, ledger_commits, exc)
        result["superseded_dead_trial"] = True
    trial = space.get(trial_id)
    assert trial is not None

    result.update(trial_id=trial_id)
    result["status"] = adjudicate_verdict(space, trial_id, ledger_rows)

    if result["status"] == TRIAL_STATUS_VOIDED:
        # A voided trial's idea stays untried -- it reached no terminal status, so the
        # cross-model lesson gate below must not see it.
        return result

    # R17: the idea's status is now whatever adjudicate_verdict's side effects landed
    # (adopted/rejected/parked, a TERMINAL status) -- re-fetch it fresh rather than trusting
    # the pre-adjudication `idea` object, and offer it to the cross-model insight gate.
    refetched_idea = space.get(idea.id)
    if refetched_idea is not None:
        maybe_file_cross_model_lesson(space, model_id, refetched_idea, lesson_filer=lesson_filer)

    return result


def _record_close(
    space: RegistrySpace, model_id: str, close: str, *, lesson_filer: Optional[LessonFiler] = None
) -> None:
    model = _model(space, model_id)
    model.meta[CAMPAIGN_STATUS_FIELD] = CLOSE_WON if close == CLOSE_WON else CAMPAIGN_COMPLETED
    # R17: model close is the final sweep -- catch any confirmed cross-model insight that
    # wasn't already filed per-trial (e.g. the second model's confirming idea reached its
    # terminal status on a dispatch this campaign never itself made).
    sweep_cross_model_lessons(space, model_id, lesson_filer=lesson_filer)


def supervise_campaign(
    space: RegistrySpace,
    model_id: str,
    ledger_rows: dict[str, LedgerRow],
    dispatcher: Dispatcher,
    *,
    interventions: tuple[Intervention, ...] = (),
    idea_generator: Optional[IdeaGenerator] = None,
    max_dispatches: Optional[int] = None,
    lesson_filer: Optional[LessonFiler] = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Drive ``model_id``'s campaign to close, dispatching one worker per trial serially.

    Each iteration calls :func:`dispatch_trial` exactly once -- a fresh, independent
    worker session that ends with its trial -- and evaluates the close condition only
    AFTER that trial's :func:`~knowledge.ml_registry.verdict.adjudicate_verdict` side
    effects have landed on ``space``. ``max_dispatches`` is a convenience cap for
    step-by-step callers and tests; it is NOT what makes the loop terminate (see the
    module docstring's TERMINATION note). A voided trial never counts against
    ``max_trials``, but consecutive voids are themselves bounded.

    The model's ``win_condition`` is parsed ONCE, up front: a campaign whose win condition
    is not evaluable (:func:`parse_win_condition`) is REFUSED here rather than run to a
    silent first-adoption "win"; one whose win condition IS first-adoption-wins without
    having declared it (:func:`check_win_on_adoption_declared`) is warned about on stderr
    and refused outright by the campaign loop's preflight.

    Close conditions, checked in this order once a dispatch's side effects have landed:
      1. the dispatch found no candidate (backlog and discovered-idea budget both
         exhausted) -> :data:`CLOSE_BACKLOG_EXHAUSTED`.
      2. the dispatcher overran ``per_trial_seconds`` -> :data:`CLOSE_TRIAL_TIMEOUT`.
      3. the trial was ledger-voided and that makes ``max_consecutive_voids`` voids in a
         row -> :data:`CLOSE_VOID_LIMIT`; otherwise the loop continues.
      4. the trial was adopted AND the declared ``win_condition`` is met by the LEDGER's
         value for its commit -> :data:`CLOSE_WON`. An adoption that does not yet reach the
         declared target advances the baseline and the campaign keeps running.
      5. the non-voided trial count has reached ``max_trials`` -> :data:`CLOSE_MAX_TRIALS`.
    Every non-win close is recorded on the model as a completed outcome
    (:data:`CAMPAIGN_COMPLETED`); a win is recorded as :data:`CLOSE_WON`.
    """
    model = _model(space, model_id)
    max_trials = int(model.meta.get("max_trials", MODEL_DEFAULTS["max_trials"]))
    max_voids = int(model.meta.get(MAX_CONSECUTIVE_VOIDS_FIELD, DEFAULT_MAX_CONSECUTIVE_VOIDS))
    # Unevaluable first, and RAISING: check_win_on_adoption_declared parses too, but its refusal
    # is caught below, so leaving this to it would downgrade "this campaign cannot be adjudicated
    # at all" to a warning.
    parse_win_condition(model.meta.get("win_condition"))
    # Then, LOUDLY, the win condition that IS evaluable but is first-adoption-wins undeclared. Warn rather than raise HERE because this function is also how an in-flight
    # campaign RESUMES (nothing distinguishes a resume from a fresh start), and a campaign
    # already six trials deep must not be bricked by a declaration it can no longer make
    # retroactively. The refusal belongs one step earlier, before trial one is dispatched:
    # `check_win_on_adoption_declared` is that gate and af-ml-campaign-loop.sh calls it as a
    # preflight, so the unattended path -- the one that closed a campaign after ONE dispatch
    # on a +1.2-floor adoption -- does not start at all.
    try:
        check_win_on_adoption_declared(model)
    except RegistryValidationError as undeclared:
        # warnings.warn, not a print: this module's callers include the CLI, whose stdout is a
        # JSON document a caller parses. A warning reaches the operator on stderr without
        # corrupting that.
        warnings.warn(f"model {model_id}: {undeclared}", stacklevel=2)

    def close_with(close: str) -> dict[str, object]:
        _record_close(space, model_id, close, lesson_filer=lesson_filer)
        return {"history": history, "close": close}

    history: list[dict[str, object]] = []
    dispatches = 0
    while max_dispatches is None or dispatches < max_dispatches:
        result = dispatch_trial(
            space, model_id, ledger_rows, dispatcher,
            interventions=interventions, idea_generator=idea_generator, lesson_filer=lesson_filer,
            clock=clock,
        )
        dispatches += 1
        history.append(result)

        if result["candidate"] is None:
            return close_with(CLOSE_BACKLOG_EXHAUSTED)

        if result["status"] == TRIAL_STATUS_TIMED_OUT:
            return close_with(CLOSE_TRIAL_TIMEOUT)

        if result["status"] == TRIAL_STATUS_VOIDED:
            # Does not count against max_trials -- but a harness that keeps voiding is a
            # cost sink, and the void run is recomputed from the registry, never counted here.
            if consecutive_void_count(space, model_id) >= max_voids:
                return close_with(CLOSE_VOID_LIMIT)
            continue

        if result["status"] == VERDICT_ADOPTED and win_condition_met(
            space, model_id, str(result["trial_id"]), ledger_rows
        ):
            return close_with(CLOSE_WON)

        if non_voided_trial_count(space, model_id) >= max_trials:
            return close_with(CLOSE_MAX_TRIALS)

    return {"history": history, "close": None}
