"""Acceptance test for ticket R3 (1a3a9d6bf67c4461a8fbbd7ee85614fb):

given a job queued longer than a threshold and a job whose claim lease is
stale, a query returns both with their respective ages.
"""

from __future__ import annotations

from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.box_service_observability import StuckReason, find_stuck_jobs


def make_job(job_id: str, **overrides) -> Job:
    defaults = dict(id=job_id, project="p", snapshot="prd-p", state=JobState.QUEUED)
    defaults.update(overrides)
    return Job(**defaults)


def test_queued_and_stale_claim_jobs_are_both_reported_with_their_ages():
    queued_job = make_job("job-queued", state=JobState.QUEUED, queued_at=0.0)
    stale_claim_job = make_job(
        "job-stale-claim",
        state=JobState.CLAIMED,
        run_owner="agent-a",
        claim_heartbeat_at=50.0,
        claim_lease_ttl=100.0,
    )
    # A healthy claim (well within its TTL) must not be reported.
    healthy_job = make_job(
        "job-healthy",
        state=JobState.RUNNING,
        run_owner="agent-b",
        claim_heartbeat_at=190.0,
        claim_lease_ttl=100.0,
    )
    # A freshly-queued job under the threshold must not be reported either.
    fresh_job = make_job("job-fresh", state=JobState.QUEUED, queued_at=195.0)

    now = 200.0
    stuck = find_stuck_jobs(
        [queued_job, stale_claim_job, healthy_job, fresh_job],
        now=now,
        queued_threshold=60.0,
    )

    by_id = {entry.job.id: entry for entry in stuck}
    assert set(by_id) == {"job-queued", "job-stale-claim"}

    queued_entry = by_id["job-queued"]
    assert queued_entry.reason is StuckReason.QUEUED_AGE
    assert queued_entry.age == 200.0

    stale_entry = by_id["job-stale-claim"]
    assert stale_entry.reason is StuckReason.STALE_CLAIM_AGE
    # Age is elapsed silence since the last heartbeat (200 - 50), not the
    # overage past the TTL.
    assert stale_entry.age == 150.0


def test_queued_job_under_threshold_is_not_reported():
    job = make_job("job-1", state=JobState.QUEUED, queued_at=100.0)
    stuck = find_stuck_jobs([job], now=150.0, queued_threshold=60.0)
    assert stuck == []


def test_claimed_job_within_lease_ttl_is_not_reported():
    job = make_job(
        "job-1",
        state=JobState.CLAIMED,
        run_owner="agent-a",
        claim_heartbeat_at=100.0,
        claim_lease_ttl=100.0,
    )
    stuck = find_stuck_jobs([job], now=150.0, queued_threshold=60.0)
    assert stuck == []


def test_terminal_jobs_are_never_reported_regardless_of_age():
    job = make_job("job-1", state=JobState.COMPLETED, queued_at=0.0)
    stuck = find_stuck_jobs([job], now=1_000_000.0, queued_threshold=60.0)
    assert stuck == []


def test_queued_threshold_has_no_hardcoded_default_source():
    """Observability invariant (R70/observability-signals): the silence
    threshold is the single configured source consulted — ``find_stuck_jobs``
    takes ``queued_threshold`` as a mandatory keyword with no internal
    default, so a caller can never silently diverge from its own configured
    value by omitting the argument.
    """
    import inspect

    sig = inspect.signature(find_stuck_jobs)
    param = sig.parameters["queued_threshold"]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_stuck_job_reason_is_always_in_or_out_of_domain_classified():
    """Every signal this module reports carries an explicit, distinct
    classification (``StuckReason``) rather than an ambiguous/free-form
    label, satisfying the "every signal is classified" half of the
    observability-signals invariant for R3's contribution."""
    assert {r.value for r in StuckReason} == {"queued-age", "stale-claim-age"}
