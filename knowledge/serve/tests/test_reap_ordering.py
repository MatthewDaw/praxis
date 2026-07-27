"""The ``cleanup``-tagged ``tail-persisted-before-teardown`` check: "The final activity tail and
terminal event are persisted BEFORE the teardown call, so a failed job's evidence outlives the
session; the ordering is asserted, not just the end state" (R41's guarantee).

Two teardown paths are covered here, independent of any single path's broader acceptance
coverage, so a future teardown path that forgets the ordering fails this check specifically
rather than relying on an unrelated test noticing:

- ``box_service_cancel.cancel_job`` — the operator-cancel action (R77).
- ``box_service_worktree_cleanup.reap_and_cleanup`` — the automatic session-terminal reaper's
  worktree teardown (R40), asserted from inside the fake ``git`` runner itself so a reordering
  that still leaves the right end state would be caught.
"""

from __future__ import annotations

import subprocess

import pytest

from knowledge.serve.box_service_activity_tail import ActivityTailStore
from knowledge.serve.box_service_cancel import cancel_job
from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.box_service_worktree_cleanup import reap_and_cleanup


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
