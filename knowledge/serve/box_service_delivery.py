"""Durable per-job delivery-stage reconciliation (R62): idempotent replay of the two
irreversible integration steps — publishing the remote ref and opening the pull request — after a
crash, rather than retrying blind.

``box_service_integrate.run_integration_sequence`` records ``Job.delivery_stage``
(:class:`~knowledge.serve.box_service_models.DeliveryStage`) on the job row BEFORE each of those
steps begins. If the box service crashes mid-sequence, the durable stage tells replay roughly
where it was — but replay never TRUSTS that value blindly: it RE-DETECTS the real remote state
(does the branch already exist at origin? is a pull request already open for it?) and only uses
the stage to decide which re-detection is relevant. That is what makes replay idempotent — a
second push or a second pull-request-open is a duplicate side effect a re-detecting caller can
always avoid, whereas trusting the stage alone cannot distinguish "the step finished right before
the crash" from "the step never started".

This module is pure decision logic — no Praxis, no subprocess, no git — so every branch is
assertable without a live repo or CLI, exactly like ``box_service_reconcile``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from knowledge.serve.box_service_models import DeliveryStage


class DeliveryAction(str, Enum):
    """What replay must do next, given the durable stage and the re-detected remote state."""

    #: Nothing irreversible has happened yet — run the ordinary reset/merge/push/PR sequence.
    RUN_FULL_SEQUENCE = "run_full_sequence"
    #: The remote ref is already published and no pull request is open for it yet — skip straight
    #: to opening exactly one pull request, never pushing again.
    SKIP_PUSH_OPEN_PR = "skip_push_open_pr"
    #: A pull request is already open for the published branch — reuse it, never open a second.
    REUSE_EXISTING_PR = "reuse_existing_pr"
    #: The recorded stage and the pull request it names are both already confirmed — nothing left
    #: to do.
    ALREADY_DELIVERED = "already_delivered"
    #: The recorded stage does not reconcile with the re-detected remote state. Replay must never
    #: guess or retry blind, so the job lands needs-attention with its branch preserved.
    NEEDS_ATTENTION = "needs_attention"


@dataclass(frozen=True)
class DeliveryDecision:
    action: DeliveryAction
    reason: str | None = None
    pr_url: str | None = None


def reconcile_delivery(
    stage: DeliveryStage,
    *,
    remote_ref_exists: bool,
    existing_pr_url: str | None,
) -> DeliveryDecision:
    """Decide replay's next action from the durable ``stage`` recorded before the crash and the
    RE-DETECTED real remote state (``remote_ref_exists``, ``existing_pr_url`` — never read from
    ``stage`` itself).

    - ``NOT_STARTED`` always runs the full sequence — nothing irreversible has been attempted.
    - ``PUBLISHING`` (a crash around the push): if the remote ref does not exist, the push never
      landed, so it is safe to run the full sequence again from the top. If it DOES exist, the
      push already succeeded — replay must never push again, so it moves on to the pull request,
      reusing one if it finds it already open rather than opening a second.
    - ``OPENING_PR`` (a crash between the confirmed push and the confirmed pull request): the
      remote ref MUST exist — a missing ref here contradicts the recorded stage and is
      unreconcilable. When the ref exists, an already-open pull request is reused; otherwise
      exactly one is opened now.
    - ``DELIVERED``: the recorded pull request is authoritative when it is still findable;
      otherwise the recorded stage no longer reconciles with reality and is unreconcilable.
    """
    if stage is DeliveryStage.NOT_STARTED:
        return DeliveryDecision(DeliveryAction.RUN_FULL_SEQUENCE)

    if stage is DeliveryStage.PUBLISHING:
        if not remote_ref_exists:
            return DeliveryDecision(DeliveryAction.RUN_FULL_SEQUENCE)
        if existing_pr_url:
            return DeliveryDecision(DeliveryAction.REUSE_EXISTING_PR, pr_url=existing_pr_url)
        return DeliveryDecision(DeliveryAction.SKIP_PUSH_OPEN_PR)

    if stage is DeliveryStage.OPENING_PR:
        if not remote_ref_exists:
            return DeliveryDecision(
                DeliveryAction.NEEDS_ATTENTION,
                reason=(
                    "delivery stage recorded pull-request-opening but the published branch is "
                    "missing at the remote"
                ),
            )
        if existing_pr_url:
            return DeliveryDecision(DeliveryAction.REUSE_EXISTING_PR, pr_url=existing_pr_url)
        return DeliveryDecision(DeliveryAction.SKIP_PUSH_OPEN_PR)

    if stage is DeliveryStage.DELIVERED:
        if existing_pr_url:
            return DeliveryDecision(DeliveryAction.ALREADY_DELIVERED, pr_url=existing_pr_url)
        return DeliveryDecision(
            DeliveryAction.NEEDS_ATTENTION,
            reason="delivery stage recorded delivered but no open pull request was found",
        )

    return DeliveryDecision(
        DeliveryAction.NEEDS_ATTENTION, reason=f"unrecognized delivery stage {stage!r}"
    )
