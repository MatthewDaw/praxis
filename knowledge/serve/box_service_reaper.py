"""Automatic session-terminal reaper (R39): once a session backing a job has
reached a terminal state, the session is closed automatically, and the final
activity tail and the terminal event are persisted BEFORE the teardown call —
so the evidence for a failed job outlives the background session that
produced it.

The ordering is the ticket's own acceptance condition, not an implementation
detail: :func:`reap_terminal_session` always calls
``box_service_activity_tail.ActivityTailStore.append`` and
``box_service_terminal.reconcile_terminal_event`` before it calls
``session_launcher.SessionLauncher.terminate``, so both are durable even if
teardown itself fails partway or the box goes down immediately after.
"""

from __future__ import annotations

from dataclasses import dataclass

from knowledge.serve.box_service_activity_tail import ActivityTailStore
from knowledge.serve.box_service_models import Job
from knowledge.serve.box_service_terminal import TerminalEvent, reconcile_terminal_event
from knowledge.serve.session_launcher import SessionLauncher


@dataclass(frozen=True)
class ReapResult:
    """The outcome of one reap pass over a single job."""

    job: Job
    tail_ref: str | None
    terminated: bool


def reap_terminal_session(
    job: Job,
    event: TerminalEvent,
    requirement_facts: list[dict],
    *,
    final_tail_chunk: str,
    tail_store: ActivityTailStore,
    launcher: SessionLauncher,
) -> ReapResult:
    """Close ``job``'s session now that it has reached a terminal state
    (R39).

    Order matters and is fixed, never reordered by a caller:

    1. Persist the final activity-tail chunk (``tail_store.append``).
    2. Reconcile the discrete terminal event against ticket completeness and
       stamp ``job.terminal_at`` from the event's own timestamp, never a poll
       (``reconcile_terminal_event`` — R24).
    3. Only then tear the background session down (``launcher.terminate``),
       so no background session for the job remains in the daemon's listing
       once this returns.

    A job with no ``session_id`` yet (never launched) has nothing to tear
    down; steps 1-2 still run so its tail/terminal record is complete, and
    ``terminated`` is reported ``False``.
    """
    tail_ref = tail_store.append(job, final_tail_chunk)
    reconcile_terminal_event(job, event, requirement_facts)

    terminated = False
    if job.session_id:
        terminated = launcher.terminate(job.session_id)

    return ReapResult(job=job, tail_ref=tail_ref, terminated=terminated)
