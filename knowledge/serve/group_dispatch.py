"""Group dispatch validation (ticket 92e23ab1, R48/R50-adjacent): several jobs
dispatched together as one **group** must be dispatched as a single, validated
unit — membership is decided once, at dispatch time, and never again.

Two guarantees, both enforced HERE rather than left to the caller:

- **Consistency at dispatch.** Every member must hold its OWN, distinct prd
  snapshot (R48's "separate prd snapshots so their ticket sets are disjoint")
  and every member must share one origin and one Praxis org — a group that
  spans repos/orgs, or that names the same snapshot twice, is refused
  outright (:func:`validate_group_dispatch`), naming the offending field
  rather than silently de-duping or dropping a member.
- **Immutability after dispatch.** :class:`JobGroup` is a frozen dataclass —
  there is no setter — and the one function that names the intent to change
  membership, :func:`attempt_change_group_membership`, always refuses. So
  neither a build session nor the control surface has any path to alter a
  group's membership once :func:`dispatch_group` has fixed it, no matter
  which principal asks.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from knowledge.serve.dispatch import DispatchError, DispatchPayload


@dataclass(frozen=True)
class JobGroup:
    """Group membership as fixed, once, by the authenticated dispatching
    principal at dispatch time (R50-adjacent). Frozen: reassigning any field
    raises ``dataclasses.FrozenInstanceError``, and the module exposes no
    other mutator."""

    group_id: str
    member_snapshots: tuple[str, ...]
    dispatching_principal: str


def _first_duplicate_snapshot(payloads: Iterable[DispatchPayload]) -> str | None:
    seen: set[str] = set()
    for payload in payloads:
        if payload.snapshot in seen:
            return payload.snapshot
        seen.add(payload.snapshot)
    return None


def validate_group_dispatch(payloads: Sequence[DispatchPayload]) -> None:
    """Refuse a group dispatch whose members are inconsistent. Raises
    :class:`DispatchError` naming the offending field; never silently drops
    a member or de-dupes a shared snapshot."""
    if len(payloads) < 2:
        raise DispatchError("a group dispatch requires at least two members")

    duplicate = _first_duplicate_snapshot(payloads)
    if duplicate is not None:
        raise DispatchError(
            f"group dispatch refused: members share prd snapshot {duplicate!r}"
        )

    origins = {payload.origin_url for payload in payloads}
    if len(origins) > 1:
        raise DispatchError(
            f"group dispatch refused: members do not share one origin {sorted(origins)!r}"
        )

    orgs = {payload.org for payload in payloads}
    if len(orgs) > 1:
        raise DispatchError(
            f"group dispatch refused: members do not share one org {sorted(orgs)!r}"
        )


def dispatch_group(payloads: Sequence[DispatchPayload], *, dispatching_principal: str) -> JobGroup:
    """Dispatch several jobs as one group. Validates the group (see
    :func:`validate_group_dispatch`) before fixing membership under the
    authenticated ``dispatching_principal`` — refusing rather than dispatching
    a partially-invalid group."""
    if not dispatching_principal:
        raise DispatchError("dispatch_group requires a non-empty dispatching_principal")
    validate_group_dispatch(payloads)
    return JobGroup(
        group_id=str(uuid.uuid4()),
        member_snapshots=tuple(payload.snapshot for payload in payloads),
        dispatching_principal=dispatching_principal,
    )


def attempt_change_group_membership(group: JobGroup, new_member_snapshots: Sequence[str]) -> None:
    """Group membership is fixed at dispatch and immutable afterward: no
    build session or control-surface action may alter it. This is the sole
    entrypoint that names the intent to change membership, and it always
    refuses — the group passed in is left completely untouched."""
    raise DispatchError(
        f"group {group.group_id} membership is immutable after dispatch; "
        "refusing to change membership"
    )
