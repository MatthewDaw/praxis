"""Authorization matrix for job lifecycle operations (R52, R57).

A job row is org-scoped and every operation on it is gated by the caller's
**principal class** — never by ambient trust. Four classes exist:

- ``dispatcher`` — the session that dispatches a remote job. May create a
  job; nothing else.
- ``box_service`` — the box-service process currently holding a job's claim
  lease. Only the principal holding *this* job's lease may set it claimed,
  running, or a terminal state.
- ``operator`` — the human/system that owns (dispatched and is responsible
  for) the job. Only the owning operator may post a mailbox message or
  trigger resume.
- ``session`` — the build session's own Praxis credential (R57): the
  least-privilege, job-scoped identity handed to the af-build worker running
  *inside* a job. It is never the dispatcher, the leaseholding box-service
  process, or the owning operator, so it is refused job creation, every
  lifecycle transition (claim/running/terminal — "reap"), mailbox writes,
  and resume by the same class checks those principals are held to. Its
  only grant is writing the ticket and observation data belonging to the
  ONE job it was minted for (``principal.id == job.id``) — a different
  job's data, even in the same org, is refused exactly like a cross-org
  caller would be.

Reads are open to any principal, subject only to the org-scope guard: a
caller whose ``org_id`` does not match the job's is refused regardless of
principal class or requested action.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PrincipalKind(str, Enum):
    DISPATCHER = "dispatcher"
    BOX_SERVICE = "box_service"
    OPERATOR = "operator"
    SESSION = "session"


class JobAction(str, Enum):
    CREATE = "create"
    SET_CLAIMED = "set_claimed"
    SET_RUNNING = "set_running"
    SET_TERMINAL = "set_terminal"
    MAILBOX_WRITE = "mailbox_write"
    RESUME = "resume"
    READ = "read"
    TICKET_WRITE = "ticket_write"
    OBSERVATION_WRITE = "observation_write"


#: Lifecycle-state transitions gated to the box-service principal CURRENTLY
#: HOLDING the job's claim lease (``JobRef.lease_holder_id``).
_LEASEHOLDER_ONLY_ACTIONS = frozenset(
    {JobAction.SET_CLAIMED, JobAction.SET_RUNNING, JobAction.SET_TERMINAL}
)

#: Actions gated to the operator principal that OWNS the job (``JobRef.owner_id``).
_OWNER_ONLY_ACTIONS = frozenset({JobAction.MAILBOX_WRITE, JobAction.RESUME})

#: Actions gated to the SESSION principal minted for THIS job alone (R57): writing the
#: job's own ticket/observation data. Never satisfiable by any other principal kind, and
#: never satisfiable by a session principal whose id names a different job.
_SESSION_OWN_JOB_ACTIONS = frozenset({JobAction.TICKET_WRITE, JobAction.OBSERVATION_WRITE})


@dataclass(frozen=True)
class JobPrincipal:
    """An authenticated caller attempting a job operation."""

    kind: PrincipalKind
    id: str
    org_id: str


@dataclass(frozen=True)
class JobRef:
    """The minimal shape of a job row an authorization decision needs."""

    id: str
    org_id: str
    owner_id: str
    lease_holder_id: str | None = None


class AuthorizationError(PermissionError):
    """Raised when a principal is refused an action on a job."""


def authorize(action: JobAction, principal: JobPrincipal, job: JobRef) -> None:
    """Raise :class:`AuthorizationError` unless ``principal`` may perform
    ``action`` on ``job``.

    Enforcement order: the org-scope guard runs first and wins over every
    other rule — a cross-org caller is refused regardless of principal class,
    identity match, or requested action — then the action's own gate.
    """
    if principal.org_id != job.org_id:
        raise AuthorizationError(
            f"principal org {principal.org_id!r} does not match job org {job.org_id!r}"
        )

    if action is JobAction.CREATE:
        if principal.kind is not PrincipalKind.DISPATCHER:
            raise AuthorizationError("only the dispatching principal may create a job")
        return

    if action in _LEASEHOLDER_ONLY_ACTIONS:
        if principal.kind is not PrincipalKind.BOX_SERVICE or principal.id != job.lease_holder_id:
            raise AuthorizationError(
                "only the leaseholding box-service principal may set claimed, "
                "running, or a terminal state"
            )
        return

    if action in _OWNER_ONLY_ACTIONS:
        if principal.kind is not PrincipalKind.OPERATOR or principal.id != job.owner_id:
            raise AuthorizationError(
                "only the owning operator principal may post a mailbox message "
                "or trigger resume"
            )
        return

    if action in _SESSION_OWN_JOB_ACTIONS:
        if principal.kind is not PrincipalKind.SESSION or principal.id != job.id:
            raise AuthorizationError(
                "only the session principal minted for this job may write its "
                "own ticket or observation data"
            )
        return

    # JobAction.READ: the org-scope guard above is the only gate — any
    # principal class in the job's org may read it.
