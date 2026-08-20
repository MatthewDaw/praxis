from __future__ import annotations

from enum import Enum


class TrialStatus(str, Enum):
    RUNNING = "running"
    COMPLETE = "complete"
    ADOPTED = "adopted"
    REJECTED = "rejected"
    PARKED = "parked"
    VOIDED = "voided"
    ERRORED = "errored"
    SUPERSEDED = "superseded"


_TERMINAL = frozenset({TrialStatus.ADOPTED, TrialStatus.REJECTED, TrialStatus.PARKED,
                       TrialStatus.VOIDED, TrialStatus.ERRORED, TrialStatus.SUPERSEDED})
_FAIR = frozenset({TrialStatus.ADOPTED, TrialStatus.REJECTED, TrialStatus.PARKED})
_ANSWERS = _FAIR
_RETRYABLE = frozenset({TrialStatus.VOIDED, TrialStatus.ERRORED, TrialStatus.SUPERSEDED})


def _status(value: TrialStatus | str) -> TrialStatus:
    return value if isinstance(value, TrialStatus) else TrialStatus(value)


def terminal(value: TrialStatus | str) -> bool:
    return _status(value) in _TERMINAL


def fairly_measured(value: TrialStatus | str) -> bool:
    return _status(value) in _FAIR


def answers_question(value: TrialStatus | str) -> bool:
    return _status(value) in _ANSWERS


def retryable(value: TrialStatus | str) -> bool:
    return _status(value) in _RETRYABLE
