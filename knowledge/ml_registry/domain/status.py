"""Canonical persisted trial statuses and their lifecycle meaning."""

from __future__ import annotations

from enum import Enum


class TrialStatus(str, Enum):
    RUNNING = "running"
    COMPLETE = "complete"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STAGNANT = "stagnant"
    VOIDED = "voided"
    ERRORED = "errored"
    SUPERSEDED = "superseded"


class Verdict(str, Enum):
    ADOPTED = "adopted"
    REJECTED = "rejected"
    PARKED = "parked"
    VOIDED = "voided"


VERDICT_TO_TRIAL_STATUS = {
    Verdict.ADOPTED: TrialStatus.SUCCEEDED,
    Verdict.REJECTED: TrialStatus.FAILED,
    Verdict.PARKED: TrialStatus.STAGNANT,
    Verdict.VOIDED: TrialStatus.VOIDED,
}
TRIAL_STATUS_TO_VERDICT = {status: verdict for verdict, status in VERDICT_TO_TRIAL_STATUS.items()}

TERMINAL_TRIAL_STATUSES = frozenset({
    TrialStatus.SUCCEEDED.value, TrialStatus.FAILED.value, TrialStatus.STAGNANT.value,
    TrialStatus.VOIDED.value, TrialStatus.ERRORED.value, TrialStatus.SUPERSEDED.value,
})
FAIRLY_MEASURED_TRIAL_STATUSES = frozenset({
    TrialStatus.SUCCEEDED.value, TrialStatus.FAILED.value, TrialStatus.STAGNANT.value,
})
ANSWERING_TRIAL_STATUSES = FAIRLY_MEASURED_TRIAL_STATUSES
RETRYABLE_TRIAL_STATUSES = frozenset({
    TrialStatus.VOIDED.value, TrialStatus.ERRORED.value, TrialStatus.SUPERSEDED.value,
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
    return TRIAL_STATUS_TO_VERDICT.get(parse_trial_status(value))
