"""Unsolicited operator notification channel and its trigger conditions (R27).

The operator learns a job needs attention over a channel readable **without
opening the dashboard** — every other observation path in this system is
pull-based (R26), so a run that stalls at 11pm is otherwise still discovered
at 9am. Exactly five conditions fire a notification (the plan's R27 plus the
acceptance-condition floor's capability-probe and mailbox additions):

1. a job enters ``awaiting-human``
2. a job enters ``failed``
3. a job enters ``needs-attention``
4. a job crosses the silence threshold (``observability_signals.attention_needed``)
5. a startup capability probe refusal (``box_service_preflight.PreflightResult``)
   causes the box service to refuse to claim a job — so the operator is told
   the remote venue is down rather than left to infer it from queued age
6. a mailbox message is left undelivered past its stated threshold

This module is pure decision logic: no real network send, no Praxis I/O.
:class:`DevTransport` is the non-production implementation that stands in for
the real out-of-dashboard channel in tests and local runs — it records the
identical payload it was asked to deliver rather than performing a send.
:class:`NotificationCenter` is the single call site every trigger goes
through; it is what guarantees the "no trigger fires a notification twice
for the same occurrence" half of the acceptance condition, keyed on
``(job_id, condition, occurrence_id)`` so a retry/re-poll of the same
occurrence is a no-op rather than a duplicate delivery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class NotificationCondition(str, Enum):
    """The condition string naming the trigger, carried on every payload
    (R27: "a condition string naming that trigger")."""

    AWAITING_HUMAN = "awaiting-human"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs-attention"
    SILENCE_THRESHOLD = "silence-threshold"
    CAPABILITY_PROBE_REFUSAL = "capability-probe-refuse-to-claim"
    MAILBOX_UNDELIVERED = "mailbox-undelivered"


@dataclass(frozen=True)
class NotificationPayload:
    """What a notification carries — job identity, project, and the firing
    condition. Nothing else: in particular no destination, so a caller can
    never redirect delivery away from the operator's configured channel."""

    job_id: str
    project: str
    condition: str


class Transport(Protocol):
    """The out-of-dashboard delivery channel. A real implementation performs
    an actual send (SMS/push/email/etc); ``send`` takes only the payload —
    never a destination — so the channel's own configured destination is
    always authoritative."""

    def send(self, payload: NotificationPayload) -> None: ...


@dataclass
class DevTransport:
    """Non-production transport: records every payload it is asked to
    deliver instead of performing a real send, and surfaces it back
    identically so tests/local runs can assert exactly what would have gone
    out without needing a real destination."""

    sent: list[NotificationPayload] = field(default_factory=list)

    def send(self, payload: NotificationPayload) -> None:
        self.sent.append(payload)


@dataclass(frozen=True)
class DeliveryReceipt:
    """Proof one notification was delivered for one occurrence."""

    occurrence_key: str
    payload: NotificationPayload
    delivered_at: float


class NotificationCenter:
    """The single call site every trigger condition notifies through.

    Delivery is keyed on ``(job_id, condition, occurrence_id)``: the same
    occurrence notified twice (e.g. a re-poll observing the same
    silence-threshold crossing, or a duplicate failure-path call for the
    same terminal transition) is a no-op the second time — ``notify``
    returns ``None`` rather than delivering again — so "no trigger fires a
    notification twice for the same occurrence" holds regardless of how many
    times a caller re-observes the same underlying event.

    ``destination`` on the call signature is accepted and IGNORED by design
    (never forwarded into the payload or the transport call) — the channel's
    own configured destination is the only one ever used, so a caller can
    never redirect a notification via a payload-supplied override.
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._delivered: dict[str, DeliveryReceipt] = {}

    def notify(
        self,
        *,
        job_id: str,
        project: str,
        condition: NotificationCondition | str,
        occurrence_id: str,
        now: float,
        destination: str | None = None,  # noqa: ARG002 - intentionally ignored, see class docstring
    ) -> DeliveryReceipt | None:
        condition_str = condition.value if isinstance(condition, NotificationCondition) else str(condition)
        key = f"{job_id}:{condition_str}:{occurrence_id}"
        if key in self._delivered:
            return None
        payload = NotificationPayload(job_id=job_id, project=project, condition=condition_str)
        self._transport.send(payload)
        receipt = DeliveryReceipt(occurrence_key=key, payload=payload, delivered_at=now)
        self._delivered[key] = receipt
        return receipt

    def receipt_for(self, job_id: str, condition: NotificationCondition | str, occurrence_id: str) -> DeliveryReceipt | None:
        condition_str = condition.value if isinstance(condition, NotificationCondition) else str(condition)
        return self._delivered.get(f"{job_id}:{condition_str}:{occurrence_id}")
