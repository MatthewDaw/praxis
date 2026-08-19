"""Idea lifecycle for the af-ml-research registry (R3): adopt / park / reject, the idea
claim lease, and the two ways an adoption ends. Also the registry's query surface (R4):
the untried backlog, rejection memory, and per-axis/per-origin yield.

Builds on R2's write path (:mod:`knowledge.ml_registry.write_path`) -- these functions
mutate an already-registered :class:`~knowledge.ml_registry.write_path.Fact`'s ``meta``
in place on a :class:`~knowledge.ml_registry.write_path.RegistrySpace`, the same
JSON-persisted stand-in R2 uses for a Praxis-backed space. An idea's lifecycle status
lives in ``meta["status"]``; absence means untried (never claimed/adopted/parked/rejected).

Adoption tenure: rejecting an idea stamps it with the id of whichever idea is currently
``adopted`` for the same model (if any) -- ``rejected_under_adoption`` -- so an adoption
that ends can find every idea rejected during its tenure without a separate ledger.

An adoption ends in exactly ONE of two ways, and the difference is the whole point of the
stamp:

* :func:`supersede_adoption` -- a BETTER idea was adopted in its place. The prior adoption
  was correct when it was made, so the ideas rejected during its tenure were judged against
  a bar that really existed and STAY rejected. The prior idea becomes
  :data:`STATUS_SUPERSEDED`: no longer the active adoption, but NOT untried -- it has been
  tried, it won, and it must never re-enter the untried backlog.
* :func:`invalidate_adoption` -- the adoption itself is repudiated (R12's harness
  retirement, or R10's rejection ratchet proving the baseline it set was false). The bar it
  set never existed, so every idea rejected under its tenure is RE-QUEUED to the untried
  backlog, and the idea itself returns to untried for a fresh attempt.
"""

from __future__ import annotations

import time
from typing import Iterable

from knowledge.ml_registry.schema import IDEA, TRIAL, RegistryValidationError
from knowledge.ml_registry.write_path import Fact, RegistrySpace

STATUS_UNTRIED = "untried"
STATUS_CLAIMED = "claimed"
STATUS_ADOPTED = "adopted"
STATUS_PARKED = "parked"
STATUS_REJECTED = "rejected"
STATUS_SUPERSEDED = "superseded"

OUTCOME_SUCCEEDED = "succeeded"
TRIAL_STATUS_SUCCEEDED = "succeeded"

DEFAULT_IDEA_CLAIM_LEASE_TTL_S = 900


def _idea(space: RegistrySpace, idea_id: str) -> Fact:
    idea = space.get(idea_id)
    if idea is None or idea.category != IDEA:
        raise RegistryValidationError(f"idea {idea_id!r} was never registered", field="idea_id")
    return idea


def _status(idea: Fact) -> str:
    return str(idea.meta.get("status") or STATUS_UNTRIED)


def _claim_is_live(idea: Fact, *, now: float) -> bool:
    heartbeat, ttl = idea.meta.get("claim_heartbeat_at"), idea.meta.get("claim_lease_ttl")
    return (
        _status(idea) == STATUS_CLAIMED
        and heartbeat is not None
        and ttl is not None
        and now - float(heartbeat) <= float(ttl)
    )


def claim_idea(
    space: RegistrySpace, idea_id: str, owner: str, *,
    ttl: int = DEFAULT_IDEA_CLAIM_LEASE_TTL_S, now: float | None = None,
) -> bool:
    """Claim an idea's lease for ``owner``.

    Returns ``False`` when a LIVE lease is already held by a different owner. A STALE
    lease (heartbeat older than its ttl) is silently reclaimed -- same lease semantics
    as the ticket build lifecycle (:mod:`agent_factory.hooks._ticket_state`).
    """
    now = time.time() if now is None else now
    idea = _idea(space, idea_id)
    if _claim_is_live(idea, now=now) and idea.meta.get("claim_owner") != owner:
        return False
    idea.meta.update(status=STATUS_CLAIMED, claim_owner=owner, claim_heartbeat_at=now, claim_lease_ttl=ttl)
    return True


def heartbeat_idea_claim(space: RegistrySpace, idea_id: str, owner: str, *, now: float | None = None) -> bool:
    """Bump a held claim's heartbeat so it stays live. False if ``owner`` doesn't hold it."""
    idea = _idea(space, idea_id)
    if idea.meta.get("claim_owner") != owner or _status(idea) != STATUS_CLAIMED:
        return False
    idea.meta["claim_heartbeat_at"] = time.time() if now is None else now
    return True


def _active_adoption(space: RegistrySpace, model_id: str) -> Fact | None:
    return next(
        (f for f in space.list_facts(IDEA) if f.meta.get("model_id") == model_id and _status(f) == STATUS_ADOPTED),
        None,
    )


def active_adoption(space: RegistrySpace, model_id: str) -> Fact | None:
    """Public entrypoint for a caller outside this module (e.g. R12's harness
    retirement) that needs to find whichever idea is currently adopted for a model."""
    return _active_adoption(space, model_id)


def adopt_idea(space: RegistrySpace, idea_id: str, trial_id: str) -> None:
    """Adopt an idea from one of its own succeeded trials, recording that outcome."""
    idea = _idea(space, idea_id)
    trial = space.get(trial_id)
    if trial is None or trial.category != TRIAL or idea_id not in trial.derived_from:
        raise RegistryValidationError(
            f"trial {trial_id!r} does not derive from idea {idea_id!r}", field="trial_id"
        )
    if trial.meta.get("status") != TRIAL_STATUS_SUCCEEDED:
        raise RegistryValidationError(
            f"trial {trial_id!r} has not succeeded; adoption requires a succeeded trial", field="status"
        )
    idea.meta.update(status=STATUS_ADOPTED, outcome=OUTCOME_SUCCEEDED, adopted_trial_id=trial_id)


def park_idea(space: RegistrySpace, idea_id: str, reactivation_trigger: str) -> None:
    """Park an idea with the (non-empty) condition that later re-qualifies it for a trial."""
    if not reactivation_trigger:
        raise RegistryValidationError(
            "park requires a non-empty reactivation_trigger", field="reactivation_trigger"
        )
    _idea(space, idea_id).meta.update(status=STATUS_PARKED, reactivation_trigger=reactivation_trigger)


def is_retriable(idea: Fact, fired_triggers: Iterable[str]) -> bool:
    """True iff a parked idea's own trigger is among the triggers that have fired."""
    return _status(idea) == STATUS_PARKED and idea.meta.get("reactivation_trigger") in set(fired_triggers)


def reject_idea(space: RegistrySpace, idea_id: str, reason: str) -> None:
    """Reject an idea, stamping which adoption (if any) was active for its model at the time."""
    if not reason:
        raise RegistryValidationError("reject requires a non-empty reason", field="reason")
    idea = _idea(space, idea_id)
    active = _active_adoption(space, str(idea.meta.get("model_id")))
    idea.meta.update(
        status=STATUS_REJECTED, rejection_reason=reason,
        rejected_under_adoption=active.id if active else None,
    )


def untried_backlog(space: RegistrySpace, *, model_id: str | None = None, now: float | None = None) -> list[Fact]:
    """Ideas available for a trial: never claimed, or claimed under a now-stale lease."""
    now = time.time() if now is None else now
    return [
        idea for idea in space.list_facts(IDEA)
        if (model_id is None or idea.meta.get("model_id") == model_id)
        and (_status(idea) == STATUS_UNTRIED or (_status(idea) == STATUS_CLAIMED and not _claim_is_live(idea, now=now)))
    ]


def rejection_memory(space: RegistrySpace, *, model_id: str | None = None) -> list[tuple[Fact, str]]:
    """Every rejected idea paired with its recorded reason."""
    return [
        (idea, str(idea.meta.get("rejection_reason") or ""))
        for idea in space.list_facts(IDEA)
        if _status(idea) == STATUS_REJECTED and (model_id is None or idea.meta.get("model_id") == model_id)
    ]


def per_axis_yield(space: RegistrySpace, *, model_id: str | None = None) -> dict[str, dict[str, dict[str, int]]]:
    """Attempt and adoption counts per idea ``axis`` and per idea ``origin``.

    An idea has been "attempted" once at least one trial is registered against it
    (:data:`~knowledge.ml_registry.schema.TRIAL`'s ``idea_id``); it counts as an
    "adoption" while its status is :data:`STATUS_ADOPTED`. Returns
    ``{axis: {origin: {"attempts": n, "adoptions": n}}}`` covering every (axis, origin)
    pair that appears among matching ideas -- so a report reader can slice by either
    dimension without a second query.
    """
    attempted_idea_ids = {t.meta.get("idea_id") for t in space.list_facts(TRIAL)}
    report: dict[str, dict[str, dict[str, int]]] = {}
    for idea in space.list_facts(IDEA):
        if model_id is not None and idea.meta.get("model_id") != model_id:
            continue
        axis = str(idea.meta.get("axis"))
        origin = str(idea.meta.get("origin"))
        bucket = report.setdefault(axis, {}).setdefault(origin, {"attempts": 0, "adoptions": 0})
        if idea.id in attempted_idea_ids:
            bucket["attempts"] += 1
        if _status(idea) == STATUS_ADOPTED:
            bucket["adoptions"] += 1
    return report


def flagged_trials(space: RegistrySpace) -> list[Fact]:
    """Every trial derived from a since-rejected idea, annotated ``meta['idea_rejected']``."""
    rejected_ids = {idea.id for idea in space.list_facts(IDEA) if _status(idea) == STATUS_REJECTED}
    flagged = [t for t in space.list_facts(TRIAL) if rejected_ids.intersection(t.derived_from)]
    for trial in flagged:
        trial.meta["idea_rejected"] = True
    return flagged


def _end_adoption(space: RegistrySpace, idea_id: str, reason: str, *, status: str) -> Fact:
    """Shared preamble for both adoption endings: validate, demote to ``status``, and drop
    the adoption bookkeeping. Neither ending touches the tenure's rejections here."""
    if not reason:
        raise RegistryValidationError("adoption reversal requires a non-empty reason", field="reason")
    idea = _idea(space, idea_id)
    if _status(idea) != STATUS_ADOPTED:
        raise RegistryValidationError(f"idea {idea_id!r} is not currently adopted", field="idea_id")
    idea.meta["status"] = status
    idea.meta["reversal_reason"] = reason
    idea.meta.pop("outcome", None)
    idea.meta.pop("adopted_trial_id", None)
    return idea


def supersede_adoption(space: RegistrySpace, idea_id: str, reason: str) -> None:
    """Demote an adoption that a BETTER idea has replaced, recording ``reason``.

    The idea becomes :data:`STATUS_SUPERSEDED` -- it is no longer the active adoption, but
    it is NOT untried and never re-enters the untried backlog. Crucially, and unlike
    :func:`invalidate_adoption`, every idea rejected during its tenure is left EXACTLY as
    it was: the superseded adoption was a real bar when those rejections were made, so they
    remain legitimate and stay in rejection memory.
    """
    _end_adoption(space, idea_id, reason, status=STATUS_SUPERSEDED)


def invalidate_adoption(space: RegistrySpace, idea_id: str, reason: str) -> None:
    """Repudiate an adoption, recording ``reason``, and return every idea rejected during its
    tenure (:func:`reject_idea`'s ``rejected_under_adoption`` stamp) to the untried backlog.

    For when the adoption itself is proven wrong -- a retired harness, or R10's rejection
    ratchet -- so the bar it set is treated as never having existed. To merely replace an
    adoption with a better one, use :func:`supersede_adoption`, which keeps the tenure's
    rejections intact.
    """
    _end_adoption(space, idea_id, reason, status=STATUS_UNTRIED)
    for other in space.list_facts(IDEA):
        if other.meta.get("rejected_under_adoption") == idea_id:
            for key in ("status", "rejection_reason", "rejected_under_adoption"):
                other.meta.pop(key, None)

def reopen_idea(space: RegistrySpace, idea_id: str, reason: str) -> dict[str, object]:
    """Return an idea to UNTRIED after a verdict that was never fairly earned.

    Not a way to relitigate a verdict you dislike. This exists for the narrow case where the
    verdict was produced by a run that should never have been adjudicated -- truncated by a wall
    clock, killed, or otherwise unfair -- so the question the idea asks is still genuinely open.

    Neither existing path covers it. `retriable-ideas` only reactivates PARKED ideas whose
    `reactivation_trigger` has fired, and `invalidate-adoption` re-queues ideas rejected under an
    adoption the ratchet has invalidated. An idea rejected on the strength of a truncated run is
    neither, and without this it stays rejected forever -- which is the worst possible record,
    because a rejection is exactly what a future session reads as a question already settled.

    Observed on the first campaign to run an expensive arm: a graph model was starved by a
    30-minute budget, its later folds scored untrained, and the mean over trained and untrained
    folds was adjudicated as a -0.2766 REJECTION of the entire graph family -- the one family the
    skeleton-action-recognition literature had converged on. The verdict has since been voided at
    the trial level, but the IDEA remained rejected, so the arm could never be re-run.

    The prior verdict is preserved under `reopened_from` rather than erased. A reader must be able
    to see that this idea was judged once, on what, and why that judgement was discarded --
    otherwise reopening is indistinguishable from quietly deleting an inconvenient result.
    """
    if not reason or not reason.strip():
        raise RegistryValidationError("reopen requires a non-empty reason", field="reason")
    idea = _idea(space, idea_id)
    prior = str(idea.meta.get("status") or "untried")
    if prior == "untried":
        raise RegistryValidationError(
            f"idea {idea_id!r} is already untried; there is no verdict to reopen", field="status")

    history = list(idea.meta.get("reopened_from") or [])
    history.append({
        "status": prior,
        "reason": reason,
        "rejection_reason": idea.meta.get("rejection_reason"),
        "rejected_under_adoption": idea.meta.get("rejected_under_adoption"),
    })
    idea.meta["status"] = "untried"
    idea.meta["reopened_from"] = history

    # VOID the trials that produced the discarded verdict. Without this the verdict survives on
    # the trial, and any answer derived from trials silently re-answers the question the reopen
    # just opened -- so the arm is never re-run and the reopen is a no-op that prints OK.
    #
    # Voided is the honest status: it means the run was not fairly earned and the arm must be
    # re-run, which is exactly what a reopen asserts. It also scopes the effect to the PRIOR runs
    # -- a trial registered AFTER the reopen answers normally, so an idea does not become
    # permanently unanswerable. Measured: excluding the idea itself instead left an arm that had
    # been reopened, re-run and adjudicated still reading as unanswered, so it re-ran forever.
    for trial in space.list_facts(TRIAL):
        if str(trial.meta.get("idea_id")) == idea_id and str(trial.meta.get("status")) != "voided":
            trial.meta["status"] = "voided"
            trial.meta["void_reason"] = f"idea reopened: {reason}"
    for stale in ("rejection_reason", "rejected_under_adoption"):
        idea.meta.pop(stale, None)
    return {"idea_id": idea_id, "previous_status": prior, "reopen_count": len(history)}
