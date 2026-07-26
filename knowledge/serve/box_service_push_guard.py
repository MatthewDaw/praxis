"""Deterministic push guard: a pure, non-model function that classifies one candidate push
request as allowed or refused. It refuses:

* a target repo that differs from the job's allowlisted origin,
* a ref outside the reserved per-job branch namespace,
* a force update,
* an update of an EXISTING remote ref (a job's branch is always a first-and-only push).

This module never calls git and never decides WHETHER a push happens — it only classifies one
push request, so wiring it in front of an actual outbound-push invocation is a single function call with
no new I/O. It exists as the shared, reusable core so every push path in the system — job-worktree
pushes (R12) and the box-level integration push (R32-R34) alike — is checked by the SAME rule set
rather than a re-implementation per caller.
"""

from __future__ import annotations

from dataclasses import dataclass


class PushRefused(RuntimeError):
    """Raised for a refused push, naming exactly which rule refused it. Never silently
    swallowed — a refusal is always an exception, never a quietly-ignored return value."""


@dataclass(frozen=True)
class PushRequest:
    """One candidate push, fully resolved (no further lookups needed to judge it)."""

    ref: str
    target_repo: str
    force: bool
    existing_refs: frozenset[str]


def guard_push(request: PushRequest, *, job_namespace_prefix: str, allowlisted_origin: str) -> None:
    """Raise :class:`PushRefused` if ``request`` violates any rule; otherwise return ``None``
    (the push is allowed). Rules are checked in a fixed order so the refusal reason is
    deterministic for a request that violates more than one."""
    if request.target_repo != allowlisted_origin:
        raise PushRefused(
            f"target repo {request.target_repo!r} is not the job's allowlisted origin "
            f"{allowlisted_origin!r}"
        )
    if not request.ref.startswith(job_namespace_prefix):
        raise PushRefused(
            f"ref {request.ref!r} is outside the reserved per-job namespace {job_namespace_prefix!r}"
        )
    if request.force:
        raise PushRefused(f"force update of {request.ref!r} refused")
    if request.ref in request.existing_refs:
        raise PushRefused(f"ref {request.ref!r} already exists on the remote — refusing to update it")
