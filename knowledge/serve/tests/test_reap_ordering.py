"""Ordering proof for the box-service teardown path (R40, check
tail-persisted-before-teardown, cb71e6985d894a48b178c02cba21cfc1): the final
activity tail and the terminal event are persisted BEFORE the worktree
teardown call, not merely both present by the time the whole sequence
finishes. The assertion is on ORDER, made from inside the fake ``git``
runner itself, so a reordering that still leaves the right end state would
be caught.
"""

from __future__ import annotations

import subprocess

from knowledge.serve.box_service_activity_tail import ActivityTailStore
from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.box_service_worktree_cleanup import reap_and_cleanup


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
