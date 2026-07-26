"""Resume action (R29): trigger a resume for a remote job that did not finish, from the website
and from MCP.

A job "did not finish" iff its state is anything other than ``COMPLETED`` — queued, claimed, running,
awaiting-human, needs-attention, or failed all resume. Resuming RELAUNCHES a new background session
for the job, but under the SAME job-scoped owner id already recorded on the job row (``Job.run_owner``,
stamped once at first launch — R31), never a fresh per-session id.

WHY THE OWNER MUST STAY FIXED (the bug this module exists to close). Every ``claude --bg`` invocation
is handed a brand-new CLI ``session_id`` by the Claude Code daemon; af-build's own gate
(``agent_factory/hooks/build_completeness_gate.py``) arms only when the running session's identity
matches a ticket's live claim owner OR its whole-set run marker owner. If resume let the relaunched
session fall back to its own fresh session_id, it would own neither the prior run's ticket claims nor
its run marker: the gate would see no live claim and no matching marker, judge no build active, and go
INERT — the session ends immediately, having built nothing, while the job records itself failed. That
is worst exactly when the operator resumes promptly, hoping to recover time, not lose more of it.

The fix is identity continuity, not a Praxis mutation: the box service launches the resumed session
with ``FACTORY_TICKET_OWNER=<job.run_owner>`` in its environment (the override lane
``build_completeness_gate._session_owner`` reads ahead of the CLI session_id). With the SAME owner
string presented, the ordinary ``_ticket_state.claim`` idempotent-renew / stale-lease-reclaim path
(``hooks/_ticket_state.py::claim``) picks the prior claims back up on its own, and the run marker's
``run_owner`` already matches, so the gate arms immediately rather than going inert — no separate
"takeover" mutation is needed, because nothing about the owner ever changed.
"""

from __future__ import annotations

from collections.abc import Callable

from knowledge.serve.box_service_models import Job, JobState

#: The env var name the resumed session's ``build_completeness_gate._session_owner`` override reads
#: (see that function's docstring). Kept here as the single source of truth for the launch contract.
FACTORY_TICKET_OWNER_ENV = "FACTORY_TICKET_OWNER"

#: A job "did not finish" iff its state is anything other than COMPLETED — the acceptance condition's
#: explicit enumeration (queued, claimed, running, awaiting-human, needs-attention, failed).
RESUMABLE_STATES = frozenset(state for state in JobState if state is not JobState.COMPLETED)

#: Launch a resumed session for ``job`` and return its new CLI session id. The launcher is expected
#: to set ``FACTORY_TICKET_OWNER_ENV`` to ``job.run_owner`` in the launched process's environment —
#: that assignment is the caller's (the box service's real launcher) responsibility, not this pure
#: decision module's; ``resume_job`` only decides WHETHER a resume may proceed and updates the job row.
Launch = Callable[[Job], str]


class ResumeError(RuntimeError):
    """Raised when a resume is attempted on a job with no valid path to it."""


def can_resume(job: Job) -> bool:
    """True iff ``job`` is in a state other than ``COMPLETED`` — the operator's only signal that a
    remote job "did not finish", regardless of which non-terminal or failed state it is parked in."""
    return job.state in RESUMABLE_STATES


def resume_job(job: Job, launch: Launch) -> Job:
    """Resume ``job`` (the operator-triggered control action, callable identically from the website
    handler and the MCP tool — both are thin callers of this one function).

    Refuses (raises :class:`ResumeError`) when the job already completed, or has no job-scoped owner
    id recorded yet (a job that never successfully launched once has nothing to resume UNDER). On
    success, calls ``launch(job)`` — which the caller wires to the real session launcher, injecting
    ``job.run_owner`` as ``FACTORY_TICKET_OWNER`` — records the new session id, returns the job to
    ``running``, and clears any stale failure bookkeeping so the job reads as freshly in-flight.

    ``job.run_owner`` itself is NEVER reassigned here: continuity of that single value across every
    relaunch (first dispatch and every subsequent resume) is exactly what keeps the prior run's ticket
    claims and run marker live for the gate (see module docstring) — resuming under a new owner would
    silently defeat the fix, so this function has no code path that can produce one.
    """
    if job.state is JobState.COMPLETED:
        raise ResumeError(f"job {job.id} already completed — nothing to resume")
    if not job.run_owner:
        raise ResumeError(f"job {job.id} has no job-scoped owner id recorded — it never launched")

    job.session_id = launch(job)
    job.state = JobState.RUNNING
    job.resumable = False
    job.failure_reason = None
    return job
