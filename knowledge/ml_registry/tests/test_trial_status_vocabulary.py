from __future__ import annotations

import pytest

from knowledge.ml_registry.domain.status import (
    TrialStatus, Verdict, answers_question, fairly_measured, retryable, terminal,
    trial_status_for_verdict, verdict_for_trial_status,
)
from knowledge.ml_registry.report import idea_verdicts
from knowledge.ml_registry.write_path import RegistrySpace, register_idea, register_model, register_trial


@pytest.mark.parametrize(("status", "is_terminal", "is_fair", "answers", "may_retry"), [
    ("running", False, False, False, False),
    ("complete", False, False, False, False),
    ("succeeded", True, True, True, False),
    ("failed", True, False, False, True),
    ("voided", True, False, False, True),
    ("superseded", True, False, False, True),
])
def test_every_persisted_trial_status_has_one_explicit_lifecycle_meaning(
        status, is_terminal, is_fair, answers, may_retry):
    assert terminal(status) is is_terminal
    assert fairly_measured(status) is is_fair
    assert answers_question(status) is answers
    assert retryable(status) is may_retry


@pytest.mark.parametrize(("verdict", "status"), [
    (Verdict.ADOPTED, TrialStatus.SUCCEEDED),
    (Verdict.REJECTED, TrialStatus.SUCCEEDED),
    (Verdict.PARKED, TrialStatus.SUCCEEDED),
    (Verdict.VOIDED, TrialStatus.VOIDED),
])
def test_external_verdict_to_persisted_trial_status_mapping_is_exhaustive(verdict, status):
    assert trial_status_for_verdict(verdict) is status
    assert verdict_for_trial_status(status) is None


@pytest.mark.parametrize("status", [
    TrialStatus.RUNNING, TrialStatus.COMPLETE, TrialStatus.SUCCEEDED, TrialStatus.FAILED,
    TrialStatus.SUPERSEDED,
])
def test_statuses_without_an_adjudication_verdict_do_not_invent_one(status):
    assert verdict_for_trial_status(status) is None


def _space_with_idea():
    space = RegistrySpace()
    model_id = register_model(space, {
        "metric": "f1", "direction": "maximize", "win_condition": {"metric_at_least": .9},
        "baseline": "base", "noise_floor": .01, "baseline_throughput": 1.0,
        "diff_size_limit": 8, "max_trials": 20, "max_discovered_ideas": 0,
    })
    idea_id = register_idea(space, {"model_id": model_id, "origin": "seeded", "axis": "architecture",
                                    "description": "retry", "id": "arm"})
    return space, model_id, idea_id


def _trial(space, model_id, idea_id, commit, status):
    return register_trial(space, {"model_id": model_id, "idea_id": idea_id, "commit": commit,
                                  "status": status, "throughput": 1.0, "diff_lines": 1},
                          frozenset({commit}))


def test_latest_retry_replaces_a_nonanswer_only_after_a_fair_verdict():
    space, model_id, idea_id = _space_with_idea()
    _trial(space, model_id, idea_id, "void", "voided")
    assert idea_verdicts(space, model_id) == {}
    assert idea_verdicts(space, model_id, statuses=None) == {"arm": "voided"}
    _trial(space, model_id, idea_id, "retry", "succeeded")
    assert idea_verdicts(space, model_id) == {"arm": "succeeded"}


def test_latest_nonanswer_reopens_reporting_even_after_an_older_fair_measurement():
    space, model_id, idea_id = _space_with_idea()
    _trial(space, model_id, idea_id, "fair", "succeeded")
    _trial(space, model_id, idea_id, "reopened", "superseded")
    assert idea_verdicts(space, model_id) == {}
    assert idea_verdicts(space, model_id, statuses=None) == {"arm": "superseded"}
