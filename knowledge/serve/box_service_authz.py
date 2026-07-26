"""Authorization matrix for job control (acceptance a75ca6a9, requirement
c8b6b949911142fab8c8b2dd626e5d64::acceptance):

  - a caller that is not the leaseholding box-service principal attempting to
    set a job terminal is refused;
  - a non-owning principal attempting a mailbox write or resume is refused;
  - a cross-org read of a job row is refused.

A job's ``claim_lease`` holder (see ``box_service_models.Lease``) is both the
"leaseholding box-service principal" that may set the job terminal and the
"owning principal" that may write its mailbox or resume it — a job with no
lease has no authorized caller for any owner-only action. Pure decision
logic: no I/O, no Praxis, no subprocess — callable from the FastAPI route
layer and the box-service daemon alike.
"""

from __future__ import annotations

from knowledge.serve.box_service_models import Job


class JobAuthorizationError(PermissionError):
    """Raised when a caller is not authorized for the job action attempted."""


def _claim_holder(job: Job) -> str | None:
    return job.claim_lease.holder_id if job.claim_lease is not None else None


def _authorize_owner(job: Job, caller_id: str, *, action: str) -> None:
    holder = _claim_holder(job)
    if holder is None or caller_id != holder:
        raise JobAuthorizationError(
            f"caller {caller_id!r} does not hold job {job.id!r}'s claim lease "
            f"(holder={holder!r}); {action} refused"
        )


def authorize_set_terminal(job: Job, caller_id: str) -> None:
    """Only the leaseholding box-service principal may set a job terminal."""
    _authorize_owner(job, caller_id, action="terminal write")


def authorize_mailbox_write(job: Job, caller_id: str) -> None:
    """Only the job's owning principal may write its mailbox."""
    _authorize_owner(job, caller_id, action="mailbox write")


def authorize_resume(job: Job, caller_id: str) -> None:
    """Only the job's owning principal may resume it."""
    _authorize_owner(job, caller_id, action="resume")


def authorize_read(job: Job, caller_org: str) -> None:
    """A read of a job row is refused across an org boundary."""
    if job.org != caller_org:
        raise JobAuthorizationError(
            f"job {job.id!r} belongs to org {job.org!r}; a caller scoped to "
            f"org {caller_org!r} is refused a cross-org read"
        )
