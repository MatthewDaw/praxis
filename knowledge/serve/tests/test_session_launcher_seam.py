"""Launch, list, resume, and terminate asserted against the named
session-launcher seam (``knowledge.serve.session_launcher.SessionLauncher``)
with a fake runner standing in for ``subprocess.run`` — no real background
session is ever started, so the contract is verifiable without a live nested
CLI (the ``session-lifecycle`` check this file satisfies)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

import pytest

from knowledge.serve.box_service_models import SessionInfo
from knowledge.serve.session_launcher import SessionLauncher, SessionLauncherError


@dataclass
class FakeRunner:
    """Records every invocation and returns a scripted CompletedProcess."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    calls: list[dict] = field(default_factory=list)

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(
            args=args, returncode=self.returncode, stdout=self.stdout, stderr=self.stderr
        )


def test_launch_returns_session_id_and_never_shells_out_for_real():
    runner = FakeRunner(stdout="sess-123\n")
    launcher = SessionLauncher(runner=runner, cli="claude")

    session_id = launcher.launch(cwd="/repo/worktree", command="/af-build", name="job-1")

    assert session_id == "sess-123"
    assert runner.calls == [
        {
            "args": ["claude", "--bg", "/af-build", "--name", "job-1"],
            "cwd": "/repo/worktree",
            "capture_output": True,
            "text": True,
            "check": False,
        }
    ]


def test_launch_raises_on_nonzero_exit():
    runner = FakeRunner(returncode=1, stderr="boom")
    launcher = SessionLauncher(runner=runner)

    with pytest.raises(SessionLauncherError, match="boom"):
        launcher.launch(cwd="/repo", command="/af-build")


def test_launch_raises_when_no_session_id_produced():
    runner = FakeRunner(stdout="")
    launcher = SessionLauncher(runner=runner)

    with pytest.raises(SessionLauncherError, match="no session id"):
        launcher.launch(cwd="/repo", command="/af-build")


def test_list_parses_every_session_field():
    payload = [
        {
            "session_id": "sess-1",
            "cwd": "/repo/wt-1",
            "kind": "bg",
            "started_at": "2026-07-25T00:00:00Z",
            "name": "job-1",
            "state": "running",
        }
    ]
    runner = FakeRunner(stdout=json.dumps(payload))
    launcher = SessionLauncher(runner=runner)

    sessions = launcher.list()

    assert sessions == [
        SessionInfo(
            session_id="sess-1",
            cwd="/repo/wt-1",
            kind="bg",
            started_at="2026-07-25T00:00:00Z",
            name="job-1",
            state="running",
        )
    ]
    assert runner.calls[0]["args"] == ["claude", "agents", "--json"]


def test_list_empty_payload_is_empty_list():
    runner = FakeRunner(stdout="")
    launcher = SessionLauncher(runner=runner)

    assert launcher.list() == []


def test_list_raises_on_nonzero_exit():
    runner = FakeRunner(returncode=1, stderr="daemon unreachable")
    launcher = SessionLauncher(runner=runner)

    with pytest.raises(SessionLauncherError, match="daemon unreachable"):
        launcher.list()


def test_resume_true_on_success_false_on_failure():
    ok_runner = FakeRunner(returncode=0)
    assert SessionLauncher(runner=ok_runner).resume("sess-1") is True
    assert ok_runner.calls[0]["args"] == ["claude", "agents", "resume", "sess-1"]

    failing_runner = FakeRunner(returncode=1)
    assert SessionLauncher(runner=failing_runner).resume("sess-1") is False


def test_terminate_true_on_success_false_on_failure():
    ok_runner = FakeRunner(returncode=0)
    assert SessionLauncher(runner=ok_runner).terminate("sess-1") is True
    assert ok_runner.calls[0]["args"] == ["claude", "agents", "terminate", "sess-1"]

    failing_runner = FakeRunner(returncode=1)
    assert SessionLauncher(runner=failing_runner).terminate("sess-1") is False
