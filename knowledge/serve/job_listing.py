"""Operator-facing job listing order (R26).

The website's top level and the equivalent MCP tool must both list live jobs
so that jobs needing attention — ``awaiting-human``, ``failed``,
``needs-attention``, or a ``claimed``/``running`` job whose external
heartbeat has gone silent past the configured threshold — sort above jobs
progressing normally. This module is the single place that ordering is
decided, so the REST route (``GET /jobs`` in ``knowledge.serve.app``) and the
``praxis_list_jobs`` MCP tool can never drift on which jobs need attention.

The attention determination is built on ``observability_signals.attention_needed``,
which never reads an IN_DOMAIN (hook-fired, forgeable-by-the-build-session)
signal by construction (R20/the ``observability-signals`` check). The single
OUT_OF_DOMAIN signal available on a ``Job`` row is ``claim_heartbeat_at`` —
stamped by the box service's own external poll loop, not by the build
session — so a job can never manufacture or suppress its own attention state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.observability_signals import (
    SILENCE_THRESHOLD_S,
    ObservationSignal,
    SignalDomain,
    attention_needed,
)

#: States that always need attention regardless of heartbeat freshness (R26).
ALWAYS_ATTENTION_STATES = frozenset(
    {JobState.AWAITING_HUMAN, JobState.FAILED, JobState.NEEDS_ATTENTION}
)

#: States that can additionally need attention once their external heartbeat
#: has gone silent past the threshold (R26's "past the silence threshold").
_HEARTBEAT_CHECKED_STATES = frozenset({JobState.CLAIMED, JobState.RUNNING})


def job_needs_attention(
    job: Job, *, now: float, silence_threshold_s: float = SILENCE_THRESHOLD_S
) -> bool:
    """True iff ``job`` belongs in the attention-needing bucket (R26)."""
    if job.state in ALWAYS_ATTENTION_STATES:
        return True
    if job.state in _HEARTBEAT_CHECKED_STATES:
        signals = []
        if job.claim_heartbeat_at is not None:
            signals.append(
                ObservationSignal(
                    domain=SignalDomain.OUT_OF_DOMAIN, observed_at=job.claim_heartbeat_at
                )
            )
        return attention_needed(signals, now=now, silence_threshold_s=silence_threshold_s)
    return False


def order_by_attention(
    jobs: list[Job], *, now: float, silence_threshold_s: float = SILENCE_THRESHOLD_S
) -> list[Job]:
    """Stable-sort ``jobs`` so every attention-needing job sorts above every
    progressing one, preserving relative order within each group."""
    return sorted(
        jobs,
        key=lambda j: not job_needs_attention(j, now=now, silence_threshold_s=silence_threshold_s),
    )


@dataclass(frozen=True)
class JobSummary:
    """A per-job, JSON-serializable view for the website/MCP surfaces."""

    id: str
    project: str
    snapshot: str
    state: str
    attention_needed: bool
    failure_reason: str | None

    @staticmethod
    def of(job: Job, *, now: float, silence_threshold_s: float = SILENCE_THRESHOLD_S) -> "JobSummary":
        return JobSummary(
            id=job.id,
            project=job.project,
            snapshot=job.snapshot,
            state=job.state.value,
            attention_needed=job_needs_attention(job, now=now, silence_threshold_s=silence_threshold_s),
            failure_reason=job.failure_reason,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project": self.project,
            "snapshot": self.snapshot,
            "state": self.state,
            "attentionNeeded": self.attention_needed,
            "failureReason": self.failure_reason,
        }


def list_jobs_for_operator(
    jobs: list[Job], *, now: float, silence_threshold_s: float = SILENCE_THRESHOLD_S
) -> list[dict[str, Any]]:
    """Order ``jobs`` by attention (R26) and summarize each as a plain dict —
    the exact shape both ``GET /jobs`` and ``praxis_list_jobs`` return."""
    ordered = order_by_attention(jobs, now=now, silence_threshold_s=silence_threshold_s)
    return [
        JobSummary.of(job, now=now, silence_threshold_s=silence_threshold_s).as_dict()
        for job in ordered
    ]
