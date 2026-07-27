"""Acceptance test for R39 (0759fd63969c4a9a8ba3aa40694a2b22): a session that
has reached a terminal state is closed automatically, and the final activity
tail and terminal event are persisted BEFORE the teardown call — the
ordering is asserted, not just the end state — so the evidence for a failed
job outlives the session that produced it. No background session for the job
remains in the daemon's listing after cleanup, and a failed job's tail and
terminal event are readable afterward with the terminal timestamp sourced
from the event, never a poll.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from knowledge.serve.box_service_activity_tail import ActivityTailStore
from knowledge.serve.box_service_failures import FailureClass
from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.box_service_reaper import reap_terminal_session
from knowledge.serve.box_service_terminal import TerminalEvent
from knowledge.serve.job_authz import JobPrincipal, PrincipalKind
from knowledge.serve.session_launcher import SessionLauncher

ORG = "org-a"


def _job(**overrides) -> Job:
    defaults = dict(
        id="job-1",
        project="proj-a",
        snapshot="prd-proj-a",
        state=JobState.RUNNING,
        session_id="sess-1",
        run_owner="box-1",
        org=ORG,
    )
    defaults.update(overrides)
    return Job(**defaults)


def _principal() -> JobPrincipal:
    return JobPrincipal(kind=PrincipalKind.OPERATOR, id="operator-1", org_id=ORG)


def _req(*, build_state="incomplete", rid="R1"):
    return {
        "id": rid,
        "meta": {"scope": "mvp", "verify": "automated", "build_state": build_state},
    }


@dataclass
class OrderedFakeRunner:
    """A ``claude`` CLI stand-in that removes a session from its own
    ``list()`` result the moment ``terminate`` is called on it — so a test
    can assert "no longer in the daemon's listing after cleanup" against a
    fake that behaves like the real daemon, without starting a real
    background session."""

    live_sessions: dict[str, dict]
    calls: list[str] = field(default_factory=list)

    def __call__(self, args, **kwargs):
        self.calls.append(args[1] if len(args) > 1 else args[0])
        if args[1:3] == ["agents", "terminate"]:
            session_id = args[3]
            self.live_sessions.pop(session_id, None)
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if args[1:3] == ["agents", "--json"]:
            import json

            rows = list(self.live_sessions.values())
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=json.dumps(rows), stderr=""
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")


def _session_row(session_id: str) -> dict:
    return {
        "session_id": session_id,
        "cwd": "/repo/wt-1",
        "kind": "bg",
        "started_at": "2026-07-25T00:00:00Z",
        "name": "job-1",
        "state": "running",
    }


def test_tail_and_terminal_event_persisted_before_teardown_call():
    """The ordering itself is asserted: a spy sequence records EVERY side
    effect (tail append, terminal-event reconciliation, teardown call) and
    the teardown call must be last, never interleaved before the other two."""
    job = _job()
    event = TerminalEvent(session_id="sess-1", occurred_at=1_700_000_000.0)
    facts = [_req(build_state="finished")]

    sequence: list[str] = []

    tail_store = ActivityTailStore()
    real_append = tail_store.append

    def spying_append(job_arg, chunk):
        sequence.append("tail_append")
        return real_append(job_arg, chunk)

    tail_store.append = spying_append  # type: ignore[method-assign]

    runner = OrderedFakeRunner(live_sessions={"sess-1": _session_row("sess-1")})

    def spying_terminate(session_id):
        sequence.append("teardown")
        return real_terminate(session_id)

    launcher = SessionLauncher(runner=runner)
    real_terminate = launcher.terminate
    launcher.terminate = spying_terminate  # type: ignore[method-assign]

    result = reap_terminal_session(
        job,
        event,
        facts,
        final_tail_chunk="last output before exit\n",
        tail_store=tail_store,
        launcher=launcher,
    )

    assert sequence == ["tail_append", "teardown"]
    assert job.terminal_at == event.occurred_at
    assert result.terminated is True


def test_no_background_session_remains_in_daemon_listing_after_cleanup():
    job = _job()
    event = TerminalEvent(session_id="sess-1", occurred_at=1_700_000_001.0)
    facts = [_req(build_state="finished")]

    runner = OrderedFakeRunner(live_sessions={"sess-1": _session_row("sess-1")})
    launcher = SessionLauncher(runner=runner)
    tail_store = ActivityTailStore()

    assert any(s.session_id == "sess-1" for s in launcher.list())

    reap_terminal_session(
        job,
        event,
        facts,
        final_tail_chunk="done\n",
        tail_store=tail_store,
        launcher=launcher,
    )

    assert all(s.session_id != "sess-1" for s in launcher.list())


def test_failed_reaped_job_tail_and_terminal_event_readable_with_event_timestamp():
    """A failed job (incomplete tickets at exit) that was reaped still has
    its tail and terminal state readable afterward, with the terminal
    timestamp sourced from the discrete event — not a wall-clock read taken
    whenever this reap happened to run."""
    job = _job()
    event_time = 1_700_000_555.0
    event = TerminalEvent(session_id="sess-1", occurred_at=event_time)
    facts = [_req(build_state="incomplete")]  # ticket left open -> failed

    runner = OrderedFakeRunner(live_sessions={"sess-1": _session_row("sess-1")})
    launcher = SessionLauncher(runner=runner)
    tail_store = ActivityTailStore()

    result = reap_terminal_session(
        job,
        event,
        facts,
        final_tail_chunk="job failed here\n",
        tail_store=tail_store,
        launcher=launcher,
    )

    assert job.state == JobState.FAILED
    assert job.failure_reason == FailureClass.TICKETS_INCOMPLETE_AT_EXIT.value
    assert job.terminal_at == event_time

    principal = _principal()
    assert tail_store.read_stored(job, principal) == "job failed here\n"
    assert result.tail_ref == job.tail_ref


def test_job_never_launched_has_nothing_to_terminate_but_still_records_evidence():
    job = _job(session_id=None)
    event = TerminalEvent(session_id="", occurred_at=1_700_000_002.0)
    facts = [_req(build_state="finished")]

    runner = OrderedFakeRunner(live_sessions={})
    launcher = SessionLauncher(runner=runner)
    tail_store = ActivityTailStore()

    result = reap_terminal_session(
        job,
        event,
        facts,
        final_tail_chunk="never launched\n",
        tail_store=tail_store,
        launcher=launcher,
    )

    assert result.terminated is False
    assert job.terminal_at == event.occurred_at
    assert job.tail_ref is not None
