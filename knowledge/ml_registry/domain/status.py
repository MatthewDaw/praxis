"""Canonical persisted trial statuses and their lifecycle meaning."""

from __future__ import annotations

from enum import Enum

from .run import RunStatus


# Compatibility name for the Praxis-fact API. The standard Registry's typed RunStatus
# is the one six-value execution vocabulary; keeping a second enum would let them drift.
TrialStatus = RunStatus


class Verdict(str, Enum):
    ADOPTED = "adopted"
    REJECTED = "rejected"
    PARKED = "parked"
    VOIDED = "voided"


VERDICT_TO_TRIAL_STATUS = {
    Verdict.ADOPTED: TrialStatus.SUCCEEDED,
    Verdict.REJECTED: TrialStatus.SUCCEEDED,
    Verdict.PARKED: TrialStatus.SUCCEEDED,
    Verdict.VOIDED: TrialStatus.VOIDED,
}

TERMINAL_TRIAL_STATUSES = frozenset({
    TrialStatus.SUCCEEDED.value, TrialStatus.FAILED.value, TrialStatus.VOIDED.value,
    TrialStatus.SUPERSEDED.value,
})
FAIRLY_MEASURED_TRIAL_STATUSES = frozenset({
    TrialStatus.SUCCEEDED.value,
})
ANSWERING_TRIAL_STATUSES = FAIRLY_MEASURED_TRIAL_STATUSES
RETRYABLE_TRIAL_STATUSES = frozenset({
    TrialStatus.FAILED.value, TrialStatus.VOIDED.value, TrialStatus.SUPERSEDED.value,
})
ANSWERED_IDEA_STATUSES = frozenset({"adopted", "rejected", "parked", "superseded"})


def parse_trial_status(value: TrialStatus | str) -> TrialStatus:
    return value if isinstance(value, TrialStatus) else TrialStatus(value)


def terminal(value: TrialStatus | str) -> bool:
    return parse_trial_status(value).value in TERMINAL_TRIAL_STATUSES


def fairly_measured(value: TrialStatus | str) -> bool:
    return parse_trial_status(value).value in FAIRLY_MEASURED_TRIAL_STATUSES


def answers_question(value: TrialStatus | str) -> bool:
    return parse_trial_status(value).value in ANSWERING_TRIAL_STATUSES


def retryable(value: TrialStatus | str) -> bool:
    return parse_trial_status(value).value in RETRYABLE_TRIAL_STATUSES


def trial_status_for_verdict(value: Verdict | str) -> TrialStatus:
    verdict = value if isinstance(value, Verdict) else Verdict(value)
    return VERDICT_TO_TRIAL_STATUS[verdict]


def verdict_for_trial_status(value: TrialStatus | str) -> Verdict | None:
    """Execution state never implies an external verdict.

    Even ``voided`` is validated as a separate tag at adjudication time. Keeping
    the reverse lookup total-but-empty prevents callers from reconstructing verdict
    authority from execution state.
    """
    parse_trial_status(value)
    return None
