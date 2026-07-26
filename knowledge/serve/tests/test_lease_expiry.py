"""Acceptance test for ticket R2 (c664a4c1c71b495085f47052336c2e7a):

given two concurrent claim attempts on one queued job, exactly one succeeds
and the other is refused; given a claimed job whose heartbeat stops, after
the lease TTL the job is claimable again.
"""

from __future__ import annotations

import threading

import pytest

from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.box_service_queue import JobQueue, LeaseConflict


def make_job(job_id: str = "job-1") -> Job:
    return Job(id=job_id, project="p", snapshot="prd-p", state=JobState.QUEUED)


def test_two_concurrent_claims_exactly_one_succeeds():
    queue = JobQueue()
    queue.enqueue(make_job())

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def attempt(owner: str) -> None:
        barrier.wait()
        try:
            queue.claim("job-1", owner)
            results[owner] = "granted"
        except LeaseConflict:
            results[owner] = "refused"

    threads = [threading.Thread(target=attempt, args=(o,)) for o in ("agent-a", "agent-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == ["granted", "refused"]
    job = queue.get("job-1")
    assert job.state is JobState.CLAIMED
    assert job.run_owner in ("agent-a", "agent-b")
    # Whichever owner was granted the lease matches the recorded result.
    granted_owner = next(o for o, r in results.items() if r == "granted")
    assert job.run_owner == granted_owner


def test_second_claim_of_a_live_lease_is_refused():
    now = [0.0]
    queue = JobQueue(clock=lambda: now[0])
    queue.enqueue(make_job())

    queue.claim("job-1", "agent-a", ttl=100)
    with pytest.raises(LeaseConflict):
        queue.claim("job-1", "agent-b", ttl=100)

    job = queue.get("job-1")
    assert job.run_owner == "agent-a"


def test_stale_lease_is_claimable_again_after_ttl():
    now = [0.0]
    queue = JobQueue(clock=lambda: now[0])
    queue.enqueue(make_job())

    queue.claim("job-1", "agent-a", ttl=100)
    now[0] += 101  # past the lease TTL with no heartbeat

    job = queue.claim("job-1", "agent-b", ttl=100)
    assert job.run_owner == "agent-b"
    assert job.state is JobState.CLAIMED


def test_heartbeat_within_ttl_keeps_the_lease_live():
    now = [0.0]
    queue = JobQueue(clock=lambda: now[0])
    queue.enqueue(make_job())

    queue.claim("job-1", "agent-a", ttl=100)
    now[0] += 90
    queue.heartbeat("job-1", "agent-a")
    now[0] += 90  # would be stale from claim time, but not from the fresh heartbeat

    with pytest.raises(LeaseConflict):
        queue.claim("job-1", "agent-b", ttl=100)


def test_heartbeat_by_non_owner_after_lease_lost_is_refused():
    now = [0.0]
    queue = JobQueue(clock=lambda: now[0])
    queue.enqueue(make_job())

    queue.claim("job-1", "agent-a", ttl=100)
    now[0] += 101
    queue.claim("job-1", "agent-b", ttl=100)  # reclaimed after staleness

    with pytest.raises(LeaseConflict):
        queue.heartbeat("job-1", "agent-a")


def test_claim_unknown_job_raises_key_error():
    queue = JobQueue()
    with pytest.raises(KeyError):
        queue.claim("missing", "agent-a")
