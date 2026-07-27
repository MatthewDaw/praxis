"""Jobs-view listing (R26): the operator-facing "which jobs are live and
their states" query, ordered so jobs needing attention sort above jobs
progressing normally, and a per-job activity read for the "recent activity"
half of the same requirement.

A job needs attention (the acceptance condition's own wording) iff its state
is ``awaiting-human`` or ``failed`` (or the already-terminal ``needs-attention``,
which the name itself declares), OR it is still open (queued/claimed/running)
but has gone quiet past the silence threshold. That staleness check reads
``Job.claim_heartbeat_at``/``queued_at`` — a hook/heartbeat-fired signal — which
is IN_DOMAIN under ``docs/observation-signal-domains.md``. That is fine here:
the doc explicitly carves out "sorting the job list" as a permitted advisory
use of in-domain signals; only a terminal/control STATE TRANSITION may never
rest on one alone, and this module makes no such transition — it only orders
an already-existing state for display (see ``box_service_reconcile.py`` /
``box_service_failures.py`` for the out-of-domain-gated transition itself).

Pure decision logic, no Praxis/subprocess/I-O — mirrors every other
``box_service_*`` building block so it is unit-testable without a live box or
database.
"""

from __future__ import annotations

from dataclasses import dataclass

from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.observability_signals import SILENCE_THRESHOLD_S

#: States that unconditionally need attention, independent of staleness
#: (the acceptance condition's own "awaiting-human or failed" clause; the
#: already-terminal ``needs-attention`` state is attention-needing by name).
ATTENTION_STATES = frozenset(
    {JobState.AWAITING_HUMAN, JobState.FAILED, JobState.NEEDS_ATTENTION}
)


def needs_attention(
    job: Job, *, now: float, silence_threshold_s: float = SILENCE_THRESHOLD_S
) -> bool:
    """True iff ``job`` needs operator attention: its state is one of
    :data:`ATTENTION_STATES`, or it is open (queued/claimed/running) and
    silent past ``silence_threshold_s`` since its last observed heartbeat
    (falling back to ``queued_at`` for a job never yet claimed).

    A job with no observed timestamp at all (neither heartbeat nor
    ``queued_at``) has nothing to measure staleness against, so it is judged
    solely on its state.
    """
    if job.state in ATTENTION_STATES:
        return True
    if not job.is_open():
        return False  # completed, or another at-rest state not in ATTENTION_STATES
    last_seen = job.claim_heartbeat_at if job.claim_heartbeat_at is not None else job.queued_at
    if last_seen is None:
        return False
    return (now - last_seen) > silence_threshold_s


@dataclass(frozen=True)
class JobViewRow:
    """One row of the ordered jobs-view listing: the job plus the derived
    attention flag the operator-facing sort key hangs on."""

    job: Job
    needs_attention: bool


def order_jobs_for_view(
    jobs: list[Job], *, now: float, silence_threshold_s: float = SILENCE_THRESHOLD_S
) -> list[JobViewRow]:
    """Every job, wrapped with its attention flag, ordered so every
    attention-needing job sorts above every job progressing normally.

    A stable sort: within each group (attention-needing / normal), jobs keep
    their input relative order — this function makes no claim about
    secondary ordering beyond the attention partition itself.
    """
    rows = [
        JobViewRow(job=j, needs_attention=needs_attention(j, now=now, silence_threshold_s=silence_threshold_s))
        for j in jobs
    ]
    return sorted(rows, key=lambda r: not r.needs_attention)
