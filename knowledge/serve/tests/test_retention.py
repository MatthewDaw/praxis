"""storage-retention check: the activity tail is org-scope authorized on
read, obeys its byte cap and rotation, is purged past its retention window,
and a deleted project space cascades its job history (R25).
"""

from __future__ import annotations

import pytest

from knowledge.serve.box_service_activity_tail import ActivityTailStore
from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.job_authz import AuthorizationError, JobPrincipal, PrincipalKind

ORG = "org-a"


def _job(job_id: str = "job-1", project: str = "proj-a") -> Job:
    return Job(
        id=job_id,
        project=project,
        snapshot=f"prd-{project}",
        state=JobState.RUNNING,
        run_owner="box-1",
        org=ORG,
    )


def _principal(org: str = ORG) -> JobPrincipal:
    return JobPrincipal(kind=PrincipalKind.OPERATOR, id="operator-1", org_id=org)


def test_read_authorized_for_matching_org_refused_cross_org():
    store = ActivityTailStore()
    job = _job()
    store.append(job, "hello")

    assert store.read_stored(job, _principal(ORG)) == "hello"
    with pytest.raises(AuthorizationError):
        store.read_stored(job, _principal("org-b"))


def test_byte_cap_and_rotation_drops_the_oldest_bytes():
    store = ActivityTailStore(byte_cap=10)
    job = _job()
    store.append(job, "0123456789")
    store.append(job, "ABCDE")

    tail = store.read_stored(job, _principal())

    assert len(tail.encode("utf-8")) == 10
    # the oldest bytes rotated out; the most recently appended bytes remain.
    assert tail.endswith("ABCDE")
    assert "0123456789" not in tail


def test_purge_past_retention_window():
    now = [1_000.0]
    store = ActivityTailStore(clock=lambda: now[0])
    job = _job()
    store.append(job, "old activity")

    now[0] += 100.0  # well within the window: not purged
    assert store.purge_expired(retention_seconds=1_000.0) == 0
    assert store.read_stored(job, _principal()) == "old activity"

    now[0] += 10_000.0  # now well past the retention window
    purged = store.purge_expired(retention_seconds=1_000.0)

    assert purged == 1
    assert store.read_stored(job, _principal()) == ""


def test_deleted_project_space_cascades_its_job_history():
    store = ActivityTailStore()
    job_a = _job("job-1", project="proj-a")
    job_b = _job("job-2", project="proj-b")
    store.append(job_a, "a's activity")
    store.append(job_b, "b's activity")

    deleted = store.delete_project("proj-a")

    assert deleted == 1
    assert store.read_stored(job_a, _principal()) == ""
    assert store.read_stored(job_b, _principal()) == "b's activity"
