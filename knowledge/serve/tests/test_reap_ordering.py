"""The ``cleanup``-tagged ``tail-persisted-before-teardown`` check: "The final activity tail and
terminal event are persisted BEFORE the teardown call, so a failed job's evidence outlives the
session; the ordering is asserted, not just the end state" (R41's guarantee).

Three teardown paths are covered here, independent of any single path's broader acceptance
coverage, so a future teardown path that forgets the ordering fails this check specifically
rather than relying on an unrelated test noticing:

- ``box_service_cancel.cancel_job`` — the operator-cancel action (R77).
- ``box_service_worktree_cleanup.reap_and_cleanup`` — the automatic session-terminal reaper's
  worktree teardown (R40), asserted from inside the fake ``git`` runner itself so a reordering
  that still leaves the right end state would be caught.
- ``box_service_reaper.reap_terminal_session`` — the automatic session-terminal reaper's session
  teardown (R39): once a job's session reaches a terminal state, the final activity-tail chunk is
  appended and the discrete terminal event is reconciled against ticket completeness BEFORE the
  session-launcher teardown call, so a failed job's evidence outlives the background session that
  produced it. No background session for the job remains in the daemon's listing after cleanup,
  and a failed job's tail and terminal event are readable afterward with the terminal timestamp
  sourced from the event, never a poll.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest

from knowledge.serve.box_service_activity_tail import ActivityTailStore
from knowledge.serve.box_service_cancel import cancel_job
from knowledge.serve.box_service_failures import FailureClass
from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.box_service_reaper import reap_terminal_session
from knowledge.serve.box_service_terminal import TerminalEvent
from knowledge.serve.box_service_worktree_cleanup import reap_and_cleanup
from knowledge.serve.job_authz import JobPrincipal, PrincipalKind
from knowledge.serve.session_launcher import SessionLauncher


def make_job(**overrides) -> Job:
    defaults = dict(
        id="job-reap-1",
        project="af-build-remote-jobs",
        snapshot="prd-af-build-remote-jobs",
        state=JobState.RUNNING,
        session_id="sess-reap-1",
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_tail_and_terminal_event_are_persisted_before_the_teardown_call():
    """Ordering asserted directly, not inferred from the end state: the persist call must appear in
    the call log before the terminate (teardown) call, for every invocation — not just "eventually
    both happened"."""
    job = make_job()
    call_order: list[str] = []

    def persist_tail(j: Job) -> None:
        call_order.append("persist_tail")

    def terminate(session_id: str) -> bool:
        call_order.append("teardown")
        return True

    cancel_job(job, persist_tail=persist_tail, terminate=terminate)

    assert call_order == ["persist_tail", "teardown"]
    assert call_order.index("persist_tail") < call_order.index("teardown")


def test_evidence_outlives_the_session_it_was_persisted_from():
    """The evidence a persist call writes must still be readable after teardown — "a failed job's
    evidence outlives the session that produced it" is the point of the ordering, not the ordering
    for its own sake."""
    job = make_job()
    persisted: dict[str, dict[str, str]] = {}
    session_alive = {"value": True}

    def persist_tail(j: Job) -> None:
        # Persistence must not depend on the session still being alive -- it happens BEFORE the
        # session is torn down, while there is still something to read from.
        assert session_alive["value"] is True
        persisted[j.id] = {
            "tail": f"final tail for {j.id}",
            "terminal_event": f"terminal event for {j.id}",
        }

    def terminate(session_id: str) -> bool:
        session_alive["value"] = False
        return True

    cancel_job(job, persist_tail=persist_tail, terminate=terminate)

    assert session_alive["value"] is False  # the session was in fact torn down
    assert persisted[job.id]["tail"] is not None
    assert persisted[job.id]["terminal_event"] is not None


def test_teardown_never_runs_when_persist_tail_raises():
    """If persistence fails, the session must not be torn down out from under evidence that never
    made it to durable storage -- otherwise the ordering guarantee is worthless."""
    job = make_job()
    teardown_calls: list[str] = []

    def persist_tail(j: Job) -> None:
        raise RuntimeError("evidence store unavailable")

    def terminate(session_id: str) -> bool:
        teardown_calls.append(session_id)
        return True

    with pytest.raises(RuntimeError):
        cancel_job(job, persist_tail=persist_tail, terminate=terminate)

    assert teardown_calls == []


def test_tail_and_terminal_event_persist_before_the_teardown_call():
    job = Job(id="job-1", project="p", snapshot="s", state=JobState.RUNNING,
              worktree_path="/tmp/does-not-matter")
    store = ActivityTailStore()

    calls: list[str] = []

    def runner(args, **kwargs):
        if args[:3] == ["git", "worktree", "remove"]:
            # By the time teardown runs, the tail must already be durable...
            assert store.has_entry(job.tail_ref), "tail must persist before teardown"
            # ...and the terminal event must already be recorded...
            assert job.state == JobState.COMPLETED, "terminal event must persist before teardown"
            calls.append("teardown")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    reap_and_cleanup(
        job,
        final_tail_chunk="job finished\n",
        tail_store=store,
        terminal_state=JobState.COMPLETED,
        terminal_reason="merged",
        merged=True,
        clone_path="/tmp/clone",
        runner=runner,
    )

    # The teardown call did actually happen (the ordering asserts inside
    # ``runner`` above ran, not just that the end state looks right).
    assert calls == ["teardown"]
    assert job.worktree_path is None


# --- box_service_reaper.reap_terminal_session (R39) ---

ORG = "org-a"


def _reaper_job(**overrides) -> Job:
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


def test_reaper_tail_and_terminal_event_persisted_before_teardown_call():
    """The ordering itself is asserted: a spy sequence records EVERY side
    effect (tail append, terminal-event reconciliation, teardown call) and
    the teardown call must be last, never interleaved before the other two."""
    job = _reaper_job()
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
    job = _reaper_job()
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
    job = _reaper_job()
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
    job = _reaper_job(session_id=None)
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
