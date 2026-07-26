"""Acceptance test for the job authorization matrix
(c8b6b949911142fab8c8b2dd626e5d64::acceptance):

given a caller that is not the leaseholding box-service principal attempting
to set a job terminal, the write is refused; given a non-owning principal
attempting a mailbox write or resume, it is refused; given a cross-org read
of a job row, it is refused.
"""

from __future__ import annotations

import pytest

from knowledge.serve.box_service_authz import (
    JobAuthorizationError,
    authorize_mailbox_write,
    authorize_read,
    authorize_resume,
    authorize_set_terminal,
)
from knowledge.serve.box_service_models import Job, JobState, Lease


def make_job(**overrides) -> Job:
    defaults = dict(
        id="job-1",
        project="af-build-remote-jobs",
        snapshot="prd-af-build-remote-jobs",
        state=JobState.RUNNING,
        org="org-a",
        claim_lease=Lease(holder_id="box-service-a", heartbeat_at=0.0, expires_at=1e12),
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_leaseholder_may_set_job_terminal():
    job = make_job()
    authorize_set_terminal(job, "box-service-a")  # does not raise


def test_non_leaseholder_is_refused_setting_job_terminal():
    job = make_job()
    with pytest.raises(JobAuthorizationError):
        authorize_set_terminal(job, "box-service-b")


def test_job_with_no_lease_refuses_every_terminal_write():
    job = make_job(claim_lease=None)
    with pytest.raises(JobAuthorizationError):
        authorize_set_terminal(job, "anyone")


def test_owning_principal_may_write_mailbox_and_resume():
    job = make_job()
    authorize_mailbox_write(job, "box-service-a")  # does not raise
    authorize_resume(job, "box-service-a")  # does not raise


def test_non_owning_principal_is_refused_mailbox_write():
    job = make_job()
    with pytest.raises(JobAuthorizationError):
        authorize_mailbox_write(job, "box-service-b")


def test_non_owning_principal_is_refused_resume():
    job = make_job()
    with pytest.raises(JobAuthorizationError):
        authorize_resume(job, "box-service-b")


def test_same_org_read_is_allowed():
    job = make_job(org="org-a")
    authorize_read(job, "org-a")  # does not raise


def test_cross_org_read_is_refused():
    job = make_job(org="org-a")
    with pytest.raises(JobAuthorizationError):
        authorize_read(job, "org-b")
