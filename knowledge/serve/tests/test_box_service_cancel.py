"""R77 acceptance: "Given a job that is running but silent, invoking cancel from either surface
terminates its session, and afterward the tail and a terminal event are readable, the job reads
needs-attention with reason operator-cancelled, and both its branch and worktree still exist."

``cancel_job`` is the one function both surfaces (website handler, MCP tool) call — see
``box_service_cancel``'s module docstring — so exercising it directly covers "either surface": there
is no per-surface branching left to diverge.
"""

from __future__ import annotations

import pytest

from knowledge.serve.box_service_cancel import (
    OPERATOR_CANCELLED,
    CancelError,
    can_cancel,
    cancel_job,
)
from knowledge.serve.box_service_models import Job, JobState


def make_job(**overrides) -> Job:
    defaults = dict(
        id="job-cancel-1",
        project="af-build-remote-jobs",
        snapshot="prd-af-build-remote-jobs",
        state=JobState.RUNNING,
        session_id="sess-running-silent",
        run_owner="af-build-remote-jobs:job-cancel-1",
        worktree_path="/var/box/worktrees/job-cancel-1",
    )
    defaults.update(overrides)
    return Job(**defaults)


class _FakeEvidenceStore:
    """Records persisted tails/terminal events per job id, so "the tail and a terminal event are
    readable" afterward is an assertable fact rather than a side effect nobody checks."""

    def __init__(self) -> None:
        self.tails: dict[str, str] = {}
        self.terminal_events: dict[str, str] = {}

    def persist(self, job: Job) -> None:
        self.tails[job.id] = f"final activity tail for {job.id}"
        self.terminal_events[job.id] = f"terminal event for {job.id}"

    def read_tail(self, job_id: str) -> str | None:
        return self.tails.get(job_id)

    def read_terminal_event(self, job_id: str) -> str | None:
        return self.terminal_events.get(job_id)


@pytest.mark.parametrize(
    "state",
    [JobState.QUEUED, JobState.CLAIMED, JobState.RUNNING, JobState.AWAITING_HUMAN],
)
def test_every_open_state_with_a_live_session_is_cancellable(state):
    job = make_job(state=state)
    assert can_cancel(job) is True


def test_running_but_silent_job_cancelled_from_either_surface_reads_needs_attention():
    """The acceptance scenario, exercised twice — once "from the website", once "from MCP" — both
    calling the SAME ``cancel_job`` (there is no other entry point for either surface to diverge
    through), on two independent jobs so the assertions don't shadow each other."""
    store = _FakeEvidenceStore()
    terminated_session_ids: list[str] = []

    def terminate(session_id: str) -> bool:
        terminated_session_ids.append(session_id)
        return True

    for surface, job in [
        ("website", make_job(id="job-website", worktree_path="/var/box/worktrees/job-website")),
        ("mcp", make_job(id="job-mcp", worktree_path="/var/box/worktrees/job-mcp")),
    ]:
        cancel_job(job, persist_tail=store.persist, terminate=terminate)

        # terminates its session
        assert job.session_id in terminated_session_ids, surface

        # afterward the tail and a terminal event are readable
        assert store.read_tail(job.id) is not None, surface
        assert store.read_terminal_event(job.id) is not None, surface

        # the job reads needs-attention with reason operator-cancelled
        assert job.state == JobState.NEEDS_ATTENTION, surface
        assert job.failure_reason == OPERATOR_CANCELLED, surface

        # both its branch and worktree still exist — cancel never clears worktree_path, and never
        # touches git, so the branch (which this pure module has no way to delete) is untouched too
        assert job.worktree_path == f"/var/box/worktrees/{job.id}", surface


def test_persist_tail_and_terminal_event_happen_before_teardown():
    """The ordering half of R41's guarantee, upheld for cancel too: persist, then terminate — never
    the other order, so a cancelled job's evidence can never be lost to a session that already died."""
    job = make_job()
    call_order: list[str] = []

    def persist_tail(j: Job) -> None:
        call_order.append(f"persist:{j.id}")

    def terminate(session_id: str) -> bool:
        call_order.append(f"terminate:{session_id}")
        return True

    cancel_job(job, persist_tail=persist_tail, terminate=terminate)

    assert call_order == [f"persist:{job.id}", f"terminate:{job.session_id}"]


def test_cancel_refuses_a_job_with_no_live_session():
    job = make_job(state=JobState.QUEUED, session_id=None)
    assert can_cancel(job) is False

    with pytest.raises(CancelError):
        cancel_job(
            job,
            persist_tail=lambda j: pytest.fail("must not persist for a job with no live session"),
            terminate=lambda sid: pytest.fail("must not terminate a job with no live session"),
        )


@pytest.mark.parametrize("state", [JobState.COMPLETED, JobState.FAILED, JobState.NEEDS_ATTENTION])
def test_cancel_refuses_an_already_terminal_job(state):
    job = make_job(state=state)
    assert can_cancel(job) is False

    with pytest.raises(CancelError):
        cancel_job(
            job,
            persist_tail=lambda j: pytest.fail("must not persist for an already-terminal job"),
            terminate=lambda sid: pytest.fail("must not terminate an already-terminal job"),
        )


def test_cancel_never_reassigns_the_job_scoped_owner_id():
    """Cancel is a control action, not a relaunch — ``run_owner`` (R31/R2's lease identity) must be
    left exactly as it was, unlike resume which relaunches under the same owner."""
    job = make_job()
    owner_before = job.run_owner

    cancel_job(job, persist_tail=lambda j: None, terminate=lambda sid: True)

    assert job.run_owner == owner_before
