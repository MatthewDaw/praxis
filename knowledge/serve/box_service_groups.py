"""Job groups (R48, R49, R50): several jobs on one repo dispatched as a
group share a `Job.group_id` (R50) and are integrated behind a single
barrier — group integration never runs until every member has reached a
terminal state (R48), so it happens **at most once** per group.

The resolved "partially-failed group" question (see the plan's Open
Decisions) is: integration proceeds over the **successful** members alone
once the whole batch is terminal — it neither waits for a resume nor aborts
the batch. A member in ``needs-attention`` (or ``failed``) is simply
excluded from the integration set; it is left exactly as recorded by
``box_service_failures.record_failure``, so it stays independently
resumable rather than being folded into — or blocking — the batch outcome.

This module is pure decision logic — no Praxis, no git, no subprocess —
matching ``box_service_reconcile``: the three cases in the acceptance
condition are assertable without a live CLI or database.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from knowledge.serve.box_service_models import Job, JobState

#: The only state that counts as "successful" for group integration purposes
#: (R49). ``failed`` and ``needs-attention`` members are terminal but never
#: successful, so they never contribute a branch to the merged batch.
SUCCESSFUL_JOB_STATES = frozenset({JobState.COMPLETED})


def members_of_group(jobs: Iterable[Job], group_id: str) -> list[Job]:
    """Every job whose ``group_id`` matches, in the order given. This is the
    "querying the group returns exactly its member jobs" half of the
    acceptance condition — the query is exhaustive, not a similarity match."""
    return [job for job in jobs if job.group_id == group_id]


@dataclass(frozen=True)
class GroupIntegrationDecision:
    """``members`` is the ordered set of SUCCESSFUL jobs group integration
    should run over — may be empty if every member failed or needs
    attention, in which case there is simply nothing to merge."""

    members: list[Job]


def plan_group_integration(group_members: Iterable[Job]) -> GroupIntegrationDecision | None:
    """Decide whether group integration may run now.

    Returns ``None`` while any member is still open (:meth:`Job.is_open`) —
    the barrier (R48) has not opened, so no integration or PR has occurred.
    Once every member is terminal, returns a decision naming the successful
    members (R49) — excluding ``failed``/``needs-attention`` members without
    withholding integration on their account, which is what keeps a
    needs-attention member independently resumable instead of blocking or
    aborting the batch.
    """
    members = list(group_members)
    if any(job.is_open() for job in members):
        return None
    successful = [job for job in members if job.state in SUCCESSFUL_JOB_STATES]
    return GroupIntegrationDecision(members=successful)
