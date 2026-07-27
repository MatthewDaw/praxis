"""Acceptance test for R22 (7469f916ab604ad8a897d4a714d4733a): a last-activity
timestamp is maintained from harness-fired hook events alone -- the external
session poll (``SessionInfo``, R21) carries only a start time, no activity
time -- and the box-service silence threshold is a single named, readable
configuration value (default 1800s) that is the sole source every
silence-based conclusion consults.
"""

from __future__ import annotations

import pytest

from knowledge.serve.box_service_activity import (
    SILENCE_THRESHOLD_S,
    HookEvent,
    is_silent,
    job_view,
    record_hook_activity,
)
from knowledge.serve.box_service_models import Job, JobState

NOW = 10_000.0


def _job(**overrides) -> Job:
    defaults = dict(id="job-1", project="p", snapshot="s", state=JobState.RUNNING)
    defaults.update(overrides)
    return Job(**defaults)


def test_harness_fired_hook_event_advances_last_activity_with_no_model_write():
    job = _job(last_activity_at=None)

    record_hook_activity(job, HookEvent.POST_TOOL_USE, now=NOW)

    assert job.last_activity_at == NOW


def test_only_recognized_harness_event_kinds_can_advance_the_timestamp():
    # A session cannot advance its own last-activity by naming an arbitrary,
    # self-declared "event" -- only the harness's own enumerated hook events
    # are honored (R20: observation must not depend on the session's
    # cooperation).
    job = _job(last_activity_at=None)

    with pytest.raises((TypeError, ValueError)):
        record_hook_activity(job, "i-am-still-here", now=NOW)  # type: ignore[arg-type]

    assert job.last_activity_at is None


def test_sigstopped_session_stops_advancing_and_crosses_the_silence_threshold():
    job = _job(last_activity_at=NOW - SILENCE_THRESHOLD_S - 1)  # process SIGSTOPped a while ago

    # No further hook events fire (the process is stopped) -- the timestamp
    # never advances on its own, and enough time has passed to cross the
    # silence threshold.
    assert is_silent(job, now=NOW) is True


def test_fresh_activity_has_not_crossed_the_silence_threshold():
    job = _job(last_activity_at=NOW - 5)

    assert is_silent(job, now=NOW) is False


def test_no_activity_at_all_is_treated_as_silent():
    job = _job(last_activity_at=None)

    assert is_silent(job, now=NOW) is True


def test_silence_threshold_default_is_1800_seconds():
    assert SILENCE_THRESHOLD_S == 1800


def test_job_view_surfaces_the_single_named_silence_threshold():
    job = _job(last_activity_at=NOW - 10)

    view = job_view(job, now=NOW)

    assert view["silence_threshold_s"] == SILENCE_THRESHOLD_S
    assert view["last_activity_at"] == NOW - 10
    assert view["silent"] is False


def test_job_view_and_is_silent_agree_as_the_sole_silence_source():
    # job_view's "silent" flag and a direct is_silent() call must never
    # disagree -- both are required to consult the same sole source rather
    # than each keeping its own duplicate notion of staleness.
    job = _job(last_activity_at=NOW - SILENCE_THRESHOLD_S - 1)

    view = job_view(job, now=NOW)

    assert view["silent"] is True
    assert view["silent"] == is_silent(job, now=NOW)
