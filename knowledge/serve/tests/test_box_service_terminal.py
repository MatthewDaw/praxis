"""Acceptance test for R24 / AE9 (0cb907d217d64449ae62b4bf5d65b09a): a job's
terminal moment is captured as a discrete event rather than inferred from a
poll interval, and the recorded terminal state distinguishes completed from
failed by reconciling against ticket completeness.

- Given a session that exits while in-scope tickets remain incomplete, the
  job's terminal state is ``failed`` rather than ``completed`` (AE9).
- Given a session that exits with the in-scope set complete, the terminal
  state is ``completed``.
- In both cases the recorded terminal timestamp is the discrete event's own
  timestamp, never a value derived from when a poll happened to observe it.
"""

from __future__ import annotations

from knowledge.serve.box_service_failures import FailureClass
from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.box_service_terminal import TerminalEvent, reconcile_terminal_event


def make_job(**overrides) -> Job:
    defaults = dict(
        id="job-1",
        project="af-build-remote-jobs",
        snapshot="prd-af-build-remote-jobs",
        state=JobState.RUNNING,
    )
    defaults.update(overrides)
    return Job(**defaults)


def _req(*, build_state="incomplete", rid="R1"):
    return {
        "id": rid,
        "meta": {"scope": "mvp", "verify": "automated", "build_state": build_state},
    }


def test_session_exit_with_incomplete_tickets_records_failed_not_completed():
    job = make_job()
    event = TerminalEvent(session_id="sess-1", occurred_at=1_700_000_000.0)
    facts = [_req(build_state="incomplete")]

    reconcile_terminal_event(job, event, facts)

    assert job.state == JobState.FAILED
    assert job.failure_reason == FailureClass.TICKETS_INCOMPLETE_AT_EXIT.value


def test_session_exit_with_complete_scope_records_completed():
    job = make_job()
    event = TerminalEvent(session_id="sess-1", occurred_at=1_700_000_000.0)
    facts = [_req(build_state="finished")]

    reconcile_terminal_event(job, event, facts)

    assert job.state == JobState.COMPLETED


def test_terminal_timestamp_comes_from_the_event_not_a_poll_observation_time():
    job = make_job()
    # A poll observing this job "now" would see a very different timestamp
    # than the discrete event's own occurred_at -- the recorded terminal
    # timestamp must be the latter, never the former.
    poll_observed_at = 9_999_999_999.0
    event = TerminalEvent(session_id="sess-1", occurred_at=1_700_000_000.0)
    facts = [_req(build_state="finished")]

    reconcile_terminal_event(job, event, facts)

    assert job.terminal_at == event.occurred_at
    assert job.terminal_at != poll_observed_at


def test_terminal_timestamp_stamped_on_the_failed_branch_too():
    job = make_job()
    event = TerminalEvent(session_id="sess-1", occurred_at=1_234_567.0)
    facts = [_req(build_state="incomplete")]

    reconcile_terminal_event(job, event, facts)

    assert job.terminal_at == event.occurred_at


def test_empty_in_scope_set_is_vacuously_complete_and_terminal_state_is_completed():
    job = make_job()
    event = TerminalEvent(session_id="sess-1", occurred_at=42.0)

    reconcile_terminal_event(job, event, [])

    assert job.state == JobState.COMPLETED
    assert job.terminal_at == 42.0
