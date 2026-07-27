"""Acceptance test for ticket R13 (8451677679ec4002abc57a176106a4e8):

given a claimed job, a background session is launched and no tmux session is
created; an external non-interactive poll of the daemon listing returns that
session's id, working directory equal to the job worktree path, kind, start
time, name, and state; given the session gone, it is absent from the
listing.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

import pytest

from knowledge.serve.box_service_models import Job, JobState, SessionInfo
from knowledge.serve.box_service_session import launch_job_session, find_job_session
from knowledge.serve.session_launcher import SessionLauncher


@dataclass
class FakeRunner:
    """Records every invocation and returns a scripted CompletedProcess —
    stands in for ``subprocess.run`` so no real background session (and, in
    particular, no real ``tmux`` session) is ever created."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    calls: list[dict] = field(default_factory=list)

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(
            args=args, returncode=self.returncode, stdout=self.stdout, stderr=self.stderr
        )


def make_job(worktree_path: str = "/repo/jobs/job-1") -> Job:
    return Job(
        id="job-1",
        project="af-build-remote-jobs",
        snapshot="prd-af-build-remote-jobs",
        state=JobState.CLAIMED,
        worktree_path=worktree_path,
    )


def test_launch_starts_background_session_with_no_tmux_call():
    runner = FakeRunner(stdout="sess-1\n")
    launcher = SessionLauncher(runner=runner, cli="claude")
    job = make_job(worktree_path="/repo/jobs/job-1")

    launched = launch_job_session(job, launcher)

    assert launched is job
    assert job.session_id == "sess-1"
    assert job.state == JobState.RUNNING
    # Exactly one subprocess call happened, and it never named tmux — the
    # only external command this path can issue is the claude CLI call.
    assert len(runner.calls) == 1
    assert "tmux" not in runner.calls[0]["args"]
    assert runner.calls[0]["args"][0] == "claude"
    assert runner.calls[0]["cwd"] == "/repo/jobs/job-1"


def test_launch_without_a_worktree_path_refuses_rather_than_launch_blind():
    launcher = SessionLauncher(runner=FakeRunner(stdout="sess-1\n"))
    job = make_job(worktree_path=None)  # type: ignore[arg-type]
    job.worktree_path = None

    with pytest.raises(ValueError, match="worktree_path"):
        launch_job_session(job, launcher)


def test_external_poll_returns_session_id_cwd_kind_started_at_name_and_state():
    runner = FakeRunner(stdout="sess-1\n")
    launcher = SessionLauncher(runner=runner)
    job = make_job(worktree_path="/repo/jobs/job-1")
    launch_job_session(job, launcher)

    # The daemon listing is polled externally (claude agents --json) — a
    # non-interactive, side-effect-free read of session state.
    listing_runner = FakeRunner(
        stdout=json.dumps(
            [
                {
                    "session_id": "sess-1",
                    "cwd": "/repo/jobs/job-1",
                    "kind": "bg",
                    "started_at": "2026-07-26T00:00:00Z",
                    "name": "job-1",
                    "state": "running",
                }
            ]
        )
    )
    poll_launcher = SessionLauncher(runner=listing_runner)

    session = find_job_session(job, poll_launcher.list())

    assert session == SessionInfo(
        session_id="sess-1",
        cwd="/repo/jobs/job-1",
        kind="bg",
        started_at="2026-07-26T00:00:00Z",
        name="job-1",
        state="running",
    )
    assert listing_runner.calls[0]["args"] == ["claude", "agents", "--json"]


def test_session_gone_is_absent_from_the_listing():
    job = make_job(worktree_path="/repo/jobs/job-1")
    job.session_id = "sess-1"

    # The session no longer appears in an external poll of the daemon
    # listing (e.g. it exited and was reaped).
    empty_listing_runner = FakeRunner(stdout=json.dumps([]))
    poll_launcher = SessionLauncher(runner=empty_listing_runner)

    assert find_job_session(job, poll_launcher.list()) is None
