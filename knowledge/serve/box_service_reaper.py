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

JOB-CONTROL LEASE (R30). Before doing anything else, the reaper checks whether ``job`` currently
holds a live job-control lease (``box_service_job_control.control_lease_is_live`` — taken by
``box_service_resume.resume_job`` before it launches a relaunch). If so, the reaper takes NO
action at all this pass — not even the tail/terminal persistence — and reports ``skipped=True``:
resume already won the race for this job, so a reap "pending" for it is cancelled by having nothing
left to act on, and reaping it now would tear down the process resume just attached.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from knowledge.serve.box_service_activity_tail import ActivityTailStore
from knowledge.serve.box_service_job_control import control_lease_is_live
from knowledge.serve.box_service_models import Job
from knowledge.serve.box_service_terminal import TerminalEvent, reconcile_terminal_event
from knowledge.serve.session_launcher import SessionLauncher


@dataclass(frozen=True)
class ReapResult:
    """The outcome of one reap pass over a single job."""

    job: Job
    tail_ref: str | None
    terminated: bool
    #: True iff the reaper took no action because ``job`` held a live job-control lease (R30) — a
    #: pending reap resume raced and won, never a failure.
    skipped: bool = False


def reap_terminal_session(
    job: Job,
    event: TerminalEvent,
    requirement_facts: list[dict],
    *,
    final_tail_chunk: str,
    tail_store: ActivityTailStore,
    launcher: SessionLauncher,
    now: float | None = None,
) -> ReapResult:
    """Close ``job``'s session now that it has reached a terminal state
    (R39).

    Refuses outright (R30) when ``job`` holds a live job-control lease — resume took it first, so
    the reaper takes no action and returns ``skipped=True`` without touching the tail, the terminal
    event, or the session.

    Otherwise, order matters and is fixed, never reordered by a caller:

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
    now = time.time() if now is None else now
    if control_lease_is_live(job, now=now):
        return ReapResult(job=job, tail_ref=None, terminated=False, skipped=True)

    tail_ref = tail_store.append(job, final_tail_chunk)
    reconcile_terminal_event(job, event, requirement_facts)

    terminated = False
    if job.session_id:
        terminated = launcher.terminate(job.session_id)

    return ReapResult(job=job, tail_ref=tail_ref, terminated=terminated)
