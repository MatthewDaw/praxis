"""Last-activity tracking for a box-service job (R22): a last-activity
timestamp is maintained from harness-fired hook events alone, since the
external session poll (``SessionInfo``, R21) carries a start time but no
activity time.

The advancing side is deliberately narrow: :func:`record_hook_activity` only
accepts a member of :class:`HookEvent` -- the harness's own enumerated hook
event kinds -- so the build session itself can never bump its own
last-activity by making a plain API/message call (R20: observation must not
depend on the session's cooperation). Per ``docs/observation-signal-domains.md``
this is an IN_DOMAIN, hook-fired signal: advisory freshness only, never the
sole basis for a terminal/control decision.

``SILENCE_THRESHOLD_S`` is the single named box-service configuration value
(default 1800s, R22/R27) that :func:`is_silent` and :func:`job_view` both
consult -- the sole source every silence-based conclusion about a job's
last-activity is measured against, so "past the silence threshold" means the
same thing everywhere a job crosses it (R26 sort order, R27 notification).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from knowledge.serve.box_service_models import Job, JobState

#: The single named box-service configuration value every silence-based
#: conclusion about a job's last-activity consults (R22/R26/R27).
SILENCE_THRESHOLD_S = 1800.0

#: R78: a job stays merely "silent" (R26/R27) until it is silent past this
#: many multiples of SILENCE_THRESHOLD_S, at which point it READS as
#: needs-attention with reason "silent" and becomes eligible for the backstop
#: reaper (D2's grace window). 2x SILENCE_THRESHOLD_S equals
#: ``local_derived_job.DEFAULT_RUN_TTL_S`` (3600s), so a remote job and a
#: derived local job share one elapsed-silence window.
NEEDS_ATTENTION_SILENCE_MULTIPLE = 2
NEEDS_ATTENTION_SILENCE_THRESHOLD_S = SILENCE_THRESHOLD_S * NEEDS_ATTENTION_SILENCE_MULTIPLE


class HookEvent(str, Enum):
    """Harness-fired hook events allowed to advance a job's last-activity
    timestamp -- never a session-authored write."""

    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"
    SUBAGENT_STOP = "SubagentStop"
    PRE_COMPACT = "PreCompact"


def record_hook_activity(job: Job, event: HookEvent, *, now: float) -> Job:
    """Advance ``job.last_activity_at`` to ``now``. ``event`` must be a
    genuine :class:`HookEvent` member -- a session cannot fabricate liveness
    by naming an arbitrary string as an "event" (R20)."""
    if not isinstance(event, HookEvent):
        raise TypeError(f"record_hook_activity requires a HookEvent, got {event!r}")
    job.last_activity_at = now
    return job


def is_silent(
    job: Job,
    *,
    now: float,
    silence_threshold_s: float = SILENCE_THRESHOLD_S,
) -> bool:
    """True iff ``job`` has crossed the silence threshold: no last-activity
    timestamp at all (a SIGSTOPped process fires no further hook events, so
    the timestamp simply stops advancing), or one older than
    ``silence_threshold_s``."""
    if job.last_activity_at is None:
        return True
    return (now - job.last_activity_at) > silence_threshold_s


def job_view(job: Job, *, now: float) -> dict[str, Any]:
    """The per-job view surfaced to the operator (R26): last-activity
    timestamp alongside the silence threshold it is measured against, so the
    threshold is readable from the view itself rather than a fact every
    caller must already know."""
    return {
        "id": job.id,
        "last_activity_at": job.last_activity_at,
        "silence_threshold_s": SILENCE_THRESHOLD_S,
        "silent": is_silent(job, now=now, silence_threshold_s=SILENCE_THRESHOLD_S),
    }


def is_reaper_eligible_for_silence(job: Job, *, now: float) -> bool:
    """True iff ``job`` has been silent past
    ``NEEDS_ATTENTION_SILENCE_THRESHOLD_S`` (R78) — the read-time signal that
    makes a stalled job eligible for the backstop reaper. Pure query: never
    mutates ``job`` (D1 — stuck is reported as an observation, not a
    verdict)."""
    return is_silent(job, now=now, silence_threshold_s=NEEDS_ATTENTION_SILENCE_THRESHOLD_S)


def silence_needs_attention_view(job: Job, *, now: float) -> dict[str, Any]:
    """The needs-attention/silent projection for a remote job (R78): once
    :func:`is_reaper_eligible_for_silence` is true, the job READS as
    ``needs-attention`` with reason ``"silent"`` regardless of its persisted
    ``state`` — the actual state mutation happens only when the backstop
    reaper acts on it."""
    eligible = is_reaper_eligible_for_silence(job, now=now)
    return {
        "id": job.id,
        "state": JobState.NEEDS_ATTENTION.value if eligible else job.state.value,
        "reason": "silent" if eligible else job.failure_reason,
        "reaper_eligible": eligible,
    }
