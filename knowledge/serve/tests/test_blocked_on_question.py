"""R79: the blocked-on-question event carries the question text, it is
persisted as its own queryable field on the job (distinct from
``failure_reason``), and it stays out of the notification payload, which
carries only job id, project, and condition."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ``agent_factory`` is a separate package tree (src layout) not on the
# knowledge/serve test suite's default path; add it the same way
# ``_ticket_state`` does for the resumability module.
_AF_SRC = Path(__file__).resolve().parents[3] / "agent_factory" / "src"
if str(_AF_SRC) not in sys.path:
    sys.path.insert(0, str(_AF_SRC))

from agent_factory.event_log import EVENT_TYPES, EventLog  # noqa: E402

from knowledge.serve.box_service_models import JobState  # noqa: E402
from knowledge.serve.box_service_notify import build_notification_payload  # noqa: E402
from knowledge.serve.box_service_store import JobStore  # noqa: E402


def test_blocked_on_question_is_a_recognized_harness_event_type():
    assert "blocked_on_question" in EVENT_TYPES


def test_emitting_a_blocked_on_question_event_records_the_question_text(tmp_path):
    log = EventLog("run-1", root=tmp_path)

    event = log.append("blocked_on_question", job_id="job-1", question="Which service: A or B?")

    assert event["type"] == "blocked_on_question"
    assert event["question"] == "Which service: A or B?"


def test_question_is_queryable_as_its_own_field_on_the_job():
    store = JobStore()
    job = store.create(project="p", snapshot="s")
    job.state = JobState.RUNNING

    updated = store.enter_awaiting_human(job.id, "Which service: A or B?")

    assert updated is job
    assert updated.state == JobState.AWAITING_HUMAN
    fetched = store.get(job.id)
    assert fetched.question == "Which service: A or B?"
    # Distinct from the terminal-failure vocabulary -- never conflated.
    assert fetched.failure_reason is None


def test_entering_awaiting_human_refuses_an_empty_question():
    store = JobStore()
    job = store.create(project="p", snapshot="s")
    job.state = JobState.RUNNING

    with pytest.raises(ValueError):
        store.enter_awaiting_human(job.id, "")


def test_entering_awaiting_human_refuses_a_terminal_job():
    store = JobStore()
    job = store.create(project="p", snapshot="s")
    job.state = JobState.COMPLETED

    with pytest.raises(ValueError):
        store.enter_awaiting_human(job.id, "Which service: A or B?")


def test_resuming_clears_the_question_so_a_later_pause_never_reads_a_stale_one():
    store = JobStore()
    job = store.create(project="p", snapshot="s")
    job.state = JobState.RUNNING
    store.enter_awaiting_human(job.id, "Which service: A or B?")

    resumed = store.resume_from_awaiting_human(job.id)

    assert resumed.state == JobState.RUNNING
    assert resumed.question is None


def test_notification_payload_carries_only_job_id_project_and_condition():
    store = JobStore()
    job = store.create(project="af-build-remote-jobs", snapshot="prd-af-build-remote-jobs")
    job.state = JobState.RUNNING
    store.enter_awaiting_human(job.id, "Which service: A or B?")

    payload = build_notification_payload(job, condition="awaiting-human")

    assert payload == {
        "job_id": job.id,
        "project": "af-build-remote-jobs",
        "condition": "awaiting-human",
    }
    assert "question" not in payload
