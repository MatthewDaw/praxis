"""Acceptance test for ticket R30 (0b963ce97fb943e59bb4b0addaaa7f21):

"given a resume issued while a backstop reap is pending for the same job, the reap is cancelled and
exactly one process ends up attached to the session; given a job holding the control lease, the
reaper takes no action."

Resume and reap are mutually exclusive per job through a single serialized job-control path:
``box_service_resume.resume_job`` atomically takes the job-control lease
(``box_service_job_control``) before it launches, and ``box_service_reaper.reap_terminal_session``
refuses to act on any job holding a live one — a liveness check alone would be read-then-act against
a poll and stale by construction.
"""

from __future__ import annotations

import subprocess

from knowledge.serve.box_service_activity_tail import ActivityTailStore
from knowledge.serve.box_service_job_control import (
    CONTROL_LEASE_TTL_S,
    control_lease_is_live,
    take_control_lease,
)
from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.box_service_reaper import reap_terminal_session
from knowledge.serve.box_service_resume import resume_job
from knowledge.serve.box_service_terminal import TerminalEvent
from knowledge.serve.session_launcher import SessionLauncher

JOB_OWNER = "af-build-remote-jobs:job-lease-1"


def make_job(**overrides) -> Job:
    defaults = dict(
        id="job-lease-1",
        project="af-build-remote-jobs",
        snapshot="prd-af-build-remote-jobs",
        state=JobState.NEEDS_ATTENTION,
        session_id="old-session-id",
        run_owner=JOB_OWNER,
    )
    defaults.update(overrides)
    return Job(**defaults)


class _FakeRunner:
    """A ``claude`` CLI stand-in whose ``terminate`` call is observable, so a test can assert the
    reaper never reached the teardown call at all."""

    def __init__(self):
        self.terminate_calls: list[str] = []

    def __call__(self, args, **kwargs):
        if args[1:3] == ["agents", "terminate"]:
            self.terminate_calls.append(args[3])
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if args[1:3] == ["agents", "--json"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")


def _req(*, build_state="finished", rid="R1"):
    return {"id": rid, "meta": {"scope": "mvp", "verify": "automated", "build_state": build_state}}


# --------------------------------------------------------------------------- take_control_lease / control_lease_is_live


def test_take_control_lease_is_live_immediately_and_expires_after_ttl():
    job = make_job()
    assert control_lease_is_live(job, now=0.0) is False

    take_control_lease(job, JOB_OWNER, now=0.0)

    assert control_lease_is_live(job, now=0.0) is True
    assert control_lease_is_live(job, now=CONTROL_LEASE_TTL_S - 1) is True
    assert control_lease_is_live(job, now=CONTROL_LEASE_TTL_S + 1) is False


# --------------------------------------------------------------------------- resume takes the lease before launching


def test_resume_takes_the_control_lease_before_launch():
    job = make_job()
    seen_live_at_launch = {}

    def launch(j):
        seen_live_at_launch["live"] = control_lease_is_live(j, now=0.0)
        return "new-session-id"

    resume_job(job, launch=launch, now=0.0)

    assert seen_live_at_launch["live"] is True
    assert control_lease_is_live(job, now=0.0) is True


# --------------------------------------------------------------------------- the reaper refuses a leased job


def test_reaper_takes_no_action_on_a_job_holding_a_live_control_lease():
    job = make_job()
    take_control_lease(job, JOB_OWNER, now=0.0)

    event = TerminalEvent(session_id="old-session-id", occurred_at=1.0)
    tail_store = ActivityTailStore()
    runner = _FakeRunner()
    launcher = SessionLauncher(runner=runner)

    result = reap_terminal_session(
        job,
        event,
        [_req()],
        final_tail_chunk="should never be persisted\n",
        tail_store=tail_store,
        launcher=launcher,
        now=0.0,
    )

    assert result.skipped is True
    assert result.terminated is False
    assert runner.terminate_calls == []  # teardown never even attempted
    assert job.terminal_at is None  # terminal reconciliation never ran either
    assert not tail_store.has_entry(job.tail_ref)


def test_reaper_acts_normally_once_the_control_lease_has_expired():
    job = make_job()
    take_control_lease(job, JOB_OWNER, now=0.0, ttl=10.0)

    event = TerminalEvent(session_id="old-session-id", occurred_at=100.0)
    tail_store = ActivityTailStore()
    runner = _FakeRunner()
    launcher = SessionLauncher(runner=runner)

    result = reap_terminal_session(
        job,
        event,
        [_req()],
        final_tail_chunk="tail after lease expiry\n",
        tail_store=tail_store,
        launcher=launcher,
        now=100.0,  # well past the 10s lease
    )

    assert result.skipped is False
    assert job.terminal_at == 100.0


# --------------------------------------------------------------------------- resume vs. a pending reap: full race


def test_resume_cancels_a_pending_reap_and_exactly_one_process_ends_up_attached():
    """The acceptance condition's central scenario: a backstop reap is pending for the job (it is
    reaper-eligible and a reap call is about to run) when resume is issued. Resume must win: it takes
    the control lease and relaunches, and the concurrent reap call — arriving after resume, as it
    always would once the lease is held — takes no action, so the session the resumed process
    attached to is never torn down. Exactly one process (the resumed one) ends up attached."""
    job = make_job(state=JobState.NEEDS_ATTENTION, session_id="old-session-id")

    # Resume is issued first and wins the race (takes the lease, relaunches under a NEW session id).
    resume_job(job, launch=lambda j: "resumed-session-id", now=0.0)
    assert job.session_id == "resumed-session-id"
    assert job.state is JobState.RUNNING

    # The backstop reap that was pending for the job's PRIOR terminal read now runs concurrently —
    # too late: the lease resume just took is still live.
    event = TerminalEvent(session_id="old-session-id", occurred_at=1.0)
    tail_store = ActivityTailStore()
    runner = _FakeRunner()
    launcher = SessionLauncher(runner=runner)

    result = reap_terminal_session(
        job,
        event,
        [_req()],
        final_tail_chunk="pending reap\n",
        tail_store=tail_store,
        launcher=launcher,
        now=0.0,
    )

    assert result.skipped is True
    assert runner.terminate_calls == []  # the reaper never tore anything down
    # Exactly one process ends up attached to the session: the job row now points at the resumed
    # session id, and nothing (neither the reaper nor resume itself) tore that session down.
    assert job.session_id == "resumed-session-id"
    assert job.state is JobState.RUNNING
