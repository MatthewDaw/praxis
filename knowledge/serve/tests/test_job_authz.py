"""Authorization matrix for job lifecycle operations (R52).

Every job transition, claim, mailbox write, resume trigger, and read of job
data requires an authenticated principal of a named class: the dispatching
principal may create a job only; only the leaseholding box-service principal
may set claimed, running, or a terminal state; only the operator principal
owning the job may post a mailbox message or trigger resume. All job rows
are org-scoped and any cross-org read or write is refused.
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
OTHER_ORG = "org-b"

JOB = JobRef(
    id="job-1",
    org_id=ORG,
    owner_id="operator-1",
    lease_holder_id="box-1",
)

DISPATCHER = JobPrincipal(kind=PrincipalKind.DISPATCHER, id="dispatcher-1", org_id=ORG)
LEASEHOLDER = JobPrincipal(kind=PrincipalKind.BOX_SERVICE, id="box-1", org_id=ORG)
OTHER_BOX = JobPrincipal(kind=PrincipalKind.BOX_SERVICE, id="box-2", org_id=ORG)
OWNER = JobPrincipal(kind=PrincipalKind.OPERATOR, id="operator-1", org_id=ORG)
OTHER_OPERATOR = JobPrincipal(kind=PrincipalKind.OPERATOR, id="operator-2", org_id=ORG)


# --- create: dispatching principal only -------------------------------------------------

def test_dispatcher_may_create():
    authorize(JobAction.CREATE, DISPATCHER, JOB)  # does not raise


@pytest.mark.parametrize("principal", [LEASEHOLDER, OWNER])
def test_non_dispatcher_may_not_create(principal):
    with pytest.raises(AuthorizationError):
        authorize(JobAction.CREATE, principal, JOB)


# --- claimed / running / terminal: leaseholding box-service principal only --------------

@pytest.mark.parametrize(
    "action", [JobAction.SET_CLAIMED, JobAction.SET_RUNNING, JobAction.SET_TERMINAL]
)
def test_leaseholder_may_transition_state(action):
    authorize(action, LEASEHOLDER, JOB)  # does not raise


@pytest.mark.parametrize(
    "action", [JobAction.SET_CLAIMED, JobAction.SET_RUNNING, JobAction.SET_TERMINAL]
)
@pytest.mark.parametrize("principal", [OTHER_BOX, DISPATCHER, OWNER])
def test_non_leaseholder_state_transition_refused(action, principal):
    with pytest.raises(AuthorizationError):
        authorize(action, principal, JOB)


def test_set_terminal_by_non_leaseholder_box_service_is_refused():
    """A caller that is a box-service principal but does not hold this job's
    lease may not set a terminal state (the acceptance floor's headline case)."""
    with pytest.raises(AuthorizationError):
        authorize(JobAction.SET_TERMINAL, OTHER_BOX, JOB)


# --- mailbox write / resume: owning operator principal only -----------------------------

@pytest.mark.parametrize("action", [JobAction.MAILBOX_WRITE, JobAction.RESUME])
def test_owner_may_mailbox_write_and_resume(action):
    authorize(action, OWNER, JOB)  # does not raise


@pytest.mark.parametrize("action", [JobAction.MAILBOX_WRITE, JobAction.RESUME])
@pytest.mark.parametrize("principal", [OTHER_OPERATOR, DISPATCHER, LEASEHOLDER])
def test_non_owner_mailbox_write_and_resume_refused(action, principal):
    with pytest.raises(AuthorizationError):
        authorize(action, principal, JOB)


# --- read: any in-org principal ----------------------------------------------------------

@pytest.mark.parametrize("principal", [DISPATCHER, LEASEHOLDER, OWNER, OTHER_BOX, OTHER_OPERATOR])
def test_any_in_org_principal_may_read(principal):
    authorize(JobAction.READ, principal, JOB)  # does not raise


# --- org scoping: refused for every action, regardless of principal class ---------------

@pytest.mark.parametrize(
    "action",
    [
        JobAction.CREATE,
        JobAction.SET_CLAIMED,
        JobAction.SET_RUNNING,
        JobAction.SET_TERMINAL,
        JobAction.MAILBOX_WRITE,
        JobAction.RESUME,
        JobAction.READ,
    ],
)
def test_cross_org_is_always_refused(action):
    cross_org_principal = JobPrincipal(kind=PrincipalKind.OPERATOR, id="operator-1", org_id=OTHER_ORG)
    with pytest.raises(AuthorizationError):
        authorize(action, cross_org_principal, JOB)


def test_cross_org_read_is_refused_even_for_a_principal_with_matching_ids():
    """Same principal id/kind as the job's owner, but a different org — the
    cross-org guard must win even when every other field matches."""
    lookalike = JobPrincipal(kind=PrincipalKind.OPERATOR, id="operator-1", org_id=OTHER_ORG)
    with pytest.raises(AuthorizationError):
        authorize(JobAction.READ, lookalike, JOB)


# --- unauthenticated: no principal at all, refused (not crashed) -----------------------

@pytest.mark.parametrize(
    "action",
    [
        JobAction.CREATE,
        JobAction.SET_CLAIMED,
        JobAction.SET_RUNNING,
        JobAction.SET_TERMINAL,
        JobAction.MAILBOX_WRITE,
        JobAction.RESUME,
        JobAction.READ,
    ],
)
def test_unauthenticated_caller_is_refused_not_crashed(action):
    """An absent principal (no credential at all) is refused through the same
    ``AuthorizationError`` path as a cross-org caller — it must never surface
    as an unhandled ``AttributeError`` from touching ``principal.org_id``."""
    with pytest.raises(AuthorizationError):
        authorize(action, None, JOB)
