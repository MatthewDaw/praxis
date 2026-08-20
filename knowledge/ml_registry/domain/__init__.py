"""Canonical campaign domain vocabulary."""

from .status import (TrialStatus, Verdict, answers_question, fairly_measured, retryable,
                     terminal, trial_status_for_verdict, verdict_for_trial_status)

__all__ = ["TrialStatus", "Verdict", "answers_question", "fairly_measured", "retryable", "terminal",
           "trial_status_for_verdict", "verdict_for_trial_status"]
