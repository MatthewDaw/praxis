"""Deterministic push guard (R33): a pure, non-model function that classifies one candidate
outbound-publish request as allowed or refused.

It refuses:

* a target repo that differs from the job's allowlisted origin,
* a ref outside the reserved per-job integration-branch namespace,
* a force update,
* an update of an EXISTING remote ref (an integration branch is always a first-and-only publish).

This module never shells out and never decides WHETHER a publish happens — it only classifies one
request — so wiring it in front of the box service's actual outbound call
(``box_service_integrate.run_integration_sequence``) is a single function call with no new I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The reserved ref namespace the box service's integration publishes into — never a job's own
#: per-ticket branch namespace, never an arbitrary ref.
RESERVED_INTEGRATION_REF_PREFIX = "refs/heads/integrate/"


@dataclass(frozen=True)
class PushRequest:
    """One candidate outbound-publish request, described declaratively — never the live git
    state itself, so the guard stays pure and unit-testable with no repo on disk."""

    target_repo: str
    ref: str
    force: bool
    remote_ref_exists: bool


@dataclass(frozen=True)
class PushDecision:
    allowed: bool
    reason: str | None = None


def evaluate_push(request: PushRequest, *, allowlisted_origin: str) -> PushDecision:
    """Classify ``request`` as allowed or refused against ``allowlisted_origin``, checking the
    target repo, ref namespace, force flag, and existing-ref condition in that order so the first
    applicable reason is always the one reported."""
    if request.target_repo != allowlisted_origin:
        return PushDecision(False, "target repo differs from the job's allowlisted origin")
    if not request.ref.startswith(RESERVED_INTEGRATION_REF_PREFIX):
        return PushDecision(False, "ref is outside the reserved per-job integration namespace")
    if request.force:
        return PushDecision(False, "force update refused")
    if request.remote_ref_exists:
        return PushDecision(False, "remote ref already exists; an integration ref is first-and-only")
    return PushDecision(True)
