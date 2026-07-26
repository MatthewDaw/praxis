"""Acceptance test for R57 (session credential is least-privilege, job-scoped).

Given a build session's Praxis credential: attempts to create a job, claim
another job, trigger resume, write a mailbox message, mutate the allowlist,
or change group membership are each refused, while writes to its own job's
ticket and observation data succeed.

The session's credential is modeled as ``JobPrincipal(kind=PrincipalKind.SESSION,
id=<job.id>)`` — the af-build worker running inside a job is minted a
credential scoped to that ONE job's id. It is checked against the same
:func:`knowledge.serve.job_authz.authorize` gate every other principal class
goes through, so a session can never be granted more than a session.

Allowlist mutation and group-membership mutation are covered structurally
elsewhere (``test_origin_allowlist_governance.py`` — the allowlist module
exposes no write function at all; ``Job.group_id`` is set only at job
creation, itself gated to the dispatcher principal) — this file asserts the
session-credential half of the matrix that lives in ``job_authz``.
"""

from __future__ import annotations

import pytest

from knowledge.serve.job_authz import (
    AuthorizationError,
    JobAction,
    JobPrincipal,
    JobRef,
    PrincipalKind,
    authorize,
)

ORG = "org-a"

JOB = JobRef(id="job-1", org_id=ORG, owner_id="operator-1", lease_holder_id="box-1")
OTHER_JOB = JobRef(id="job-2", org_id=ORG, owner_id="operator-1", lease_holder_id="box-1")

#: The build session's own Praxis credential, minted for JOB alone.
SESSION = JobPrincipal(kind=PrincipalKind.SESSION, id=JOB.id, org_id=ORG)


@pytest.mark.parametrize(
    "action",
    [
        JobAction.CREATE,
        JobAction.SET_CLAIMED,
        JobAction.SET_RUNNING,
        JobAction.SET_TERMINAL,  # "reap"
        JobAction.MAILBOX_WRITE,
        JobAction.RESUME,
    ],
)
def test_session_credential_refused_every_control_action(action):
    """A build session's credential may never create a job, claim/reap another
    job, trigger resume, or post a mailbox message — on its own job or any
    other."""
    with pytest.raises(AuthorizationError):
        authorize(action, SESSION, JOB)


def test_session_credential_may_write_its_own_job_ticket_and_observation_data():
    authorize(JobAction.TICKET_WRITE, SESSION, JOB)  # does not raise
    authorize(JobAction.OBSERVATION_WRITE, SESSION, JOB)  # does not raise


@pytest.mark.parametrize("action", [JobAction.TICKET_WRITE, JobAction.OBSERVATION_WRITE])
def test_session_credential_refused_a_different_jobs_ticket_and_observation_data(action):
    """Least-privilege is JOB-scoped, not just org-scoped: the same session
    credential refuses a sibling job's data even though both jobs share an
    org."""
    with pytest.raises(AuthorizationError):
        authorize(action, SESSION, OTHER_JOB)


@pytest.mark.parametrize("action", [JobAction.TICKET_WRITE, JobAction.OBSERVATION_WRITE])
def test_non_session_principal_may_not_write_ticket_or_observation_data(action):
    """Only the SESSION principal kind may ever satisfy these actions — a
    dispatcher, box-service, or operator principal never can, even on the
    job they otherwise control."""
    dispatcher = JobPrincipal(kind=PrincipalKind.DISPATCHER, id="dispatcher-1", org_id=ORG)
    leaseholder = JobPrincipal(kind=PrincipalKind.BOX_SERVICE, id="box-1", org_id=ORG)
    owner = JobPrincipal(kind=PrincipalKind.OPERATOR, id="operator-1", org_id=ORG)
    for principal in (dispatcher, leaseholder, owner):
        with pytest.raises(AuthorizationError):
            authorize(action, principal, JOB)


def test_cross_org_session_credential_is_refused_even_for_its_own_job():
    cross_org_session = JobPrincipal(kind=PrincipalKind.SESSION, id=JOB.id, org_id="org-b")
    with pytest.raises(AuthorizationError):
        authorize(JobAction.TICKET_WRITE, cross_org_session, JOB)
