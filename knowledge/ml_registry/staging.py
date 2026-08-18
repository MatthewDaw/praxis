"""Order a campaign's backlog so coarse questions settle before fine ones.

A backlog with dependencies and a cost filter still has no ORDERING, and without one a campaign
will happily tune a hyperparameter for a model it is about to replace. That is not a small waste:
on a campaign where one arm costs minutes, an early stage can invalidate every result that came
before it, and those results are indistinguishable from valid ones afterwards.

WHAT IS SYSTEMATIC AND LIVES HERE: ordered stages as a concept, finding the open one, and the rule
that decides when a stage is closed.

WHAT IS PROJECT-SPECIFIC AND DOES NOT: the stage LIST and how an idea maps onto it. A vision
campaign's stages ("what the model sees, what the model is, how it is trained, hyperparameters,
scale") are not an LLM finetune's, and encoding one project's taxonomy here would force it on
every other. The project passes both in.

THE ONE RULE WORTH ARGUING ABOUT: a stage closes when every arm in it has a VERDICT -- adopted,
rejected, or parked -- and NOT when one is adopted. A stage that answered "none of these help" is
settled, and that is a common outcome, not a failure: the first campaign this was written for had
seven representation arms and zero adoptions, which is a real answer about the corpus. Gating on
adoption would wedge such a campaign behind any inert axis forever.

THE COST, stated because it is real and permanent: a late stage never influences an early one. If
an augmentation would only pay off under an architecture that lost, that interaction is invisible.
Staging trades interaction coverage for not spending the budget on questions whose answer is about
to be invalidated. That trade is right when arms are expensive and the backlog is long, and wrong
when arms are nearly free -- in which case do not stage at all.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

#: A verdict in any of these states counts as "this arm has been answered", so a stage holding
#: only such arms is closed. `errored` is deliberately ABSENT: an arm that crashed is not an arm
#: that lost, and letting a crash close a stage would silently shrink the campaign.
ANSWERED = frozenset({"adopted", "rejected", "parked", "voided", "superseded"})


def default_stage_of(item: dict[str, Any]) -> str:
    """An item's stage: its explicit `stage`, else its `axis`. Projects override for mappings."""
    return str(item.get("stage") or item.get("axis") or "")


def open_stage(items: Iterable[dict[str, Any]], answered_ids: set[str],
               stages: Sequence[str], *, stage_of: Callable[[dict], str] = default_stage_of,
               id_key: str = "id") -> str | None:
    """The earliest stage still holding an unanswered arm, or None when the backlog is exhausted.

    Items whose stage is not in ``stages`` are not stranded: once every named stage is closed they
    are returned in their own right, so a mis-typed or newly-invented stage surfaces as work rather
    than vanishing from the queue.
    """
    items = list(items)
    for stage in stages:
        if any(stage_of(i) == stage and i[id_key] not in answered_ids for i in items):
            return stage
    leftover = [i for i in items if i[id_key] not in answered_ids]
    return stage_of(leftover[0]) if leftover else None


def eligible(item: dict[str, Any], *, answered_ids: set[str], adopted_ids: set[str],
             stage: str | None = None, stage_of: Callable[[dict], str] = default_stage_of,
             id_key: str = "id", depends_key: str = "depends_on") -> bool:
    """Whether an arm may run now.

    Three independent gates, and they mean different things. ``answered_ids`` prevents re-running
    a settled question. ``depends_on`` expresses "this arm is only meaningful if that specific idea
    WON" -- a composition arm, say -- and so genuinely gates on adoption. ``stage`` expresses
    "this whole question is not open yet", which gates on the previous stage being ANSWERED. The
    second is about one idea's result; the third is about a phase of enquiry.
    """
    if item[id_key] in answered_ids:
        return False
    if not all(d in adopted_ids for d in (item.get(depends_key) or [])):
        return False
    return stage is None or stage_of(item) == stage


def stage_progress(items: Iterable[dict[str, Any]], answered_ids: set[str],
                   stages: Sequence[str], *, stage_of: Callable[[dict], str] = default_stage_of,
                   id_key: str = "id") -> list[dict[str, Any]]:
    """Per-stage counts, so a campaign can report where it is without recomputing eligibility."""
    items = list(items)
    out = []
    for stage in stages:
        members = [i for i in items if stage_of(i) == stage]
        done = [i for i in members if i[id_key] in answered_ids]
        out.append({"stage": stage, "total": len(members), "answered": len(done),
                    "closed": bool(members) and len(done) == len(members),
                    "empty": not members})
    return out

def unreachable(items: Iterable[dict[str, Any]], answered_ids: set[str], adopted_ids: set[str],
                *, id_key: str = "id", depends_key: str = "depends_on") -> set[str]:
    """Items that can NEVER become eligible, because a dependency will never be adopted.

    ``depends_on`` gates on a dependency being ADOPTED, so the moment that dependency is answered
    as anything else -- parked, rejected, voided -- every dependent is dead. Dead is not the same
    as unanswered, and the difference is what keeps a campaign moving: an unanswered item holds its
    stage open, so a dead one holds it open FOREVER.

    The failure mode is quiet, which is what makes it worth a function. `open_stage` keeps
    returning that stage, the eligibility filter yields an empty queue, and a supervising loop
    exits reporting nothing to do -- indistinguishable from a finished campaign. Observed on the
    first campaign to use staging: one composition arm gated on an idea that PARKED held the
    representation stage open with an empty queue, and a 27-item backlog would have stopped after
    four items and looked successful.

    Computed to a fixpoint, so a chain of dependents collapses in a single pass rather than one
    stage per invocation.

    Pass the result into ``open_stage``'s ``answered_ids`` (union it with the genuinely answered).
    Callers should REPORT what it returns rather than silently dropping it -- an item that never
    ran for a structural reason is a real omission, and one that a reader will otherwise mistake
    for an item that was tried and lost.
    """
    items = list(items)
    dead = set(answered_ids) - set(adopted_ids)
    blocked: set[str] = set()
    while True:
        grew = False
        for item in items:
            if item[id_key] in blocked:
                continue
            deps = item.get(depends_key) or []
            if any(d in dead or d in blocked for d in deps):
                blocked.add(item[id_key])
                grew = True
        if not grew:
            return blocked
