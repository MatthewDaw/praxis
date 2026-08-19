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


class StagingStuck(Exception):
    """An open stage still has unanswered arms, but none of them can run.

    That is not a finished campaign: leftover ids are holding the stage open
    (so later stages stay closed) while ``eligible`` yields an empty queue.
    The usual cause is skip-ids / out-of-scope / unreachable that the caller
    forgot to union into ``answered_ids``.
    """

    def __init__(self, stage: str, leftover: list[str]) -> None:
        self.stage = stage
        self.leftover = leftover
        super().__init__(
            f"stage {stage!r} is stuck with leftover ids {leftover}; "
            "union skip-ids / out-of-scope / unreachable into answered_ids"
        )


def next_queue(items: Iterable[dict[str, Any]], answered_ids: set[str],
               adopted_ids: set[str], stages: Sequence[str], *,
               stage_of: Callable[[dict], str] = default_stage_of,
               id_key: str = "id",
               depends_key: str = "depends_on") -> tuple[str | None, list, set[str]]:
    """Open stage, its eligible arms, and the unreachable set used to free prior stages.

    Unions ``unreachable`` into answered so a parked dependency cannot wedge the
    campaign behind an empty queue. If the resulting open stage still has
    unanswered members and none of them are eligible, raises ``StagingStuck``
    rather than returning an empty queue that a composing loop would treat as
    success.
    """
    items = list(items)
    blocked = unreachable(items, answered_ids, adopted_ids,
                          id_key=id_key, depends_key=depends_key)
    unioned = set(answered_ids) | blocked
    stage = open_stage(items, unioned, stages, stage_of=stage_of, id_key=id_key)
    if stage is None:
        return (None, [], blocked)
    queue = [i for i in items
             if eligible(i, answered_ids=unioned, adopted_ids=adopted_ids,
                         stage=stage, stage_of=stage_of,
                         id_key=id_key, depends_key=depends_key)]
    leftover = [i[id_key] for i in items
                if stage_of(i) == stage and i[id_key] not in unioned]
    if leftover and not queue:
        raise StagingStuck(stage, leftover)
    return (stage, queue, blocked)

#: A stage closing on fewer genuinely-measured arms than this has not answered its question; it
#: has merely run out of registered ones. Three is a floor, not a target -- see `stage_coverage`.
MIN_MEASURED_PER_STAGE = 3


def stage_coverage(items: Iterable[dict[str, Any]], stages: Sequence[str], *,
                   measured_ids: set[str], answered_ids: set[str] | None = None,
                   stage_of: Callable[[dict], str] = default_stage_of,
                   id_key: str = "id", min_measured: int = MIN_MEASURED_PER_STAGE
                   ) -> list[dict[str, Any]]:
    """Per-stage report of what was actually MEASURED, versus answered by some other means.

    A stage closes when every item in it is answered. Nothing checks whether it was answered by
    RUNNING anything. An item can be answered by being excluded at registration, by becoming
    unreachable, by being filtered as out of scope, or by being a no-op against the incumbent --
    and a stage made entirely of those closes having tested nothing at all.

    Observed on the first staged campaign, on the axis the stage order itself calls high-leverage:
    the architecture stage held five authored ideas and produced TWO real comparisons. One was
    excluded by a skip list, which silently killed a second through `depends_on`; a third
    re-measured the incumbent configuration and could only park. The campaign then advanced to
    augmentation, allocating it four arms -- more than the axis that decides what the model IS.

    `measured_ids` should contain only items that ran and produced a verdict from their own
    result. Do not include items answered by exclusion, unreachability, scope filtering, or a
    no-op re-measurement of the incumbent; those are exactly what this exists to make visible.

    THE NO-OP IS THE ONE THAT WILL CATCH YOU. The obvious way to build `measured_ids` -- "every
    idea that has a trial" -- silently includes an arm whose configuration was IDENTICAL to the
    incumbent, which can only ever park. On the first campaign to use this, that one arm was the
    difference between a stage reporting three measured arms and the honest count of two, which
    is the difference between passing and failing the floor. Compare each arm's resolved
    configuration against the baseline's before counting it.

    Returns one row per stage with `measured`, `answered_without_running`, and `thin`. Thin is
    advisory: this function reports, it does not block. A thin stage may be perfectly fine when
    the axis genuinely has few options, but it must be REPORTED as thin rather than presented as
    settled -- the distinction between "we tested this and it lost" and "we never tested this" is
    the whole value of a dead-ideas register.
    """
    items = list(items)
    answered = set(answered_ids) if answered_ids is not None else None
    out = []
    for stage in stages:
        members = [i for i in items if stage_of(i) == stage]
        measured = [i for i in members if i[id_key] in measured_ids]
        # A stage is CLOSED once every member is answered. Without `answered_ids` we cannot tell,
        # and fall back to treating any stage with members as closed -- the old behaviour.
        closed = (bool(members) and all(i[id_key] in answered for i in members)
                  if answered is not None else bool(members))
        out.append({
            "stage": stage,
            "total": len(members),
            "measured": len(measured),
            "measured_ids": sorted(i[id_key] for i in measured),
            "answered_without_running": len(members) - len(measured),
            "closed": closed,
            # Thin describes a stage that CLOSED on too little evidence. A stage that has not run
            # yet is not thin, it is pending, and flagging it produces alarm fatigue that trains a
            # reader to ignore the flag that matters. Measured on the first campaign to use this:
            # three of five stages were flagged purely for not having started.
            "thin": closed and len(measured) < min_measured,
        })
    return out


def thin_stages(coverage: Sequence[dict[str, Any]]) -> list[str]:
    """The stages in a `stage_coverage` report that closed on too little evidence."""
    return [c["stage"] for c in coverage if c["thin"]]
