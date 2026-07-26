"""R83: session-launcher and command-wrapper seams.

Every requirement whose behavior can only be observed on a live background
session is verified against a NAMED injectable seam rather than a real
session. This module asserts launch/list/resume/terminate against the
``SessionLauncher`` seam, and lock-wrapped invocation against the
``CommandWrapper`` seam — in both cases with a fake runner, so no real
background Claude Code session and no real host lock are ever touched by the
automated suite.

A separate, explicitly-gated manual smoke test at the bottom of this module
exercises the REAL implementations against the real ``claude`` CLI once, on
an operator's box — it is skipped by default and only runs when
``BOX_SERVICE_MANUAL_SMOKE=1`` is set.
"""

from __future__ import annotations

import json
import os

import pytest

from knowledge.serve.box_service.session_launcher import (
    ClaudeSessionLauncher,
    SessionInfo,
)
from knowledge.serve.box_service.command_wrapper import (
    AdvisoryLockCommandWrapper,
)


class FakeRunner:
    """Records every invocation and returns a canned result — the injectable
    subprocess seam. No real process is ever spawned."""

    def __init__(self, outputs=None):
        self.calls: list[list[str]] = []
        self._outputs = list(outputs or [])

    def __call__(self, argv: list[str], **kwargs):
        self.calls.append(list(argv))
        if self._outputs:
            return self._outputs.pop(0)
        return _Result(0, "", "")


class _Result:
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# --------------------------------------------------------------------- launch

def test_launch_uses_claude_bg_and_returns_session_id():
    runner = FakeRunner(outputs=[_Result(0, "sess-abc123\n", "")])
    launcher = ClaudeSessionLauncher(runner=runner)

    session_id = launcher.launch(cwd="/work/job-1", prompt="build the ticket")

    assert session_id == "sess-abc123"
    assert runner.calls, "launch must invoke the seam, never spawn a real session"
    argv = runner.calls[0]
    assert "claude" in argv[0]
    assert "--bg" in argv
    assert "--cwd" in argv and "/work/job-1" in argv


def test_launch_raises_on_nonzero_exit():
    runner = FakeRunner(outputs=[_Result(1, "", "boom")])
    launcher = ClaudeSessionLauncher(runner=runner)

    with pytest.raises(RuntimeError):
        launcher.launch(cwd="/work/job-1", prompt="build the ticket")


# ---------------------------------------------------------------------- list

def test_list_parses_daemon_json():
    payload = json.dumps(
        [
            {
                "id": "sess-abc123",
                "cwd": "/work/job-1",
                "kind": "background",
                "startedAt": "2026-07-26T00:00:00Z",
                "name": "job-1",
                "state": "running",
            }
        ]
    )
    runner = FakeRunner(outputs=[_Result(0, payload, "")])
    launcher = ClaudeSessionLauncher(runner=runner)

    sessions = launcher.list_sessions()

    assert sessions == [
        SessionInfo(
            id="sess-abc123",
            cwd="/work/job-1",
            kind="background",
            started_at="2026-07-26T00:00:00Z",
            name="job-1",
            state="running",
        )
    ]
    argv = runner.calls[0]
    assert "agents" in argv and "--json" in argv


def test_list_returns_empty_when_daemon_reports_no_sessions():
    runner = FakeRunner(outputs=[_Result(0, "[]", "")])
    launcher = ClaudeSessionLauncher(runner=runner)

    assert launcher.list_sessions() == []


# -------------------------------------------------------------------- resume

def test_resume_invokes_seam_with_session_id_and_message():
    runner = FakeRunner()
    launcher = ClaudeSessionLauncher(runner=runner)

    launcher.resume("sess-abc123", message="please continue")

    argv = runner.calls[0]
    assert "resume" in argv
    assert "sess-abc123" in argv
    assert "please continue" in argv


# ------------------------------------------------------------------ terminate

def test_terminate_invokes_seam_with_session_id():
    runner = FakeRunner()
    launcher = ClaudeSessionLauncher(runner=runner)

    launcher.terminate("sess-abc123")

    argv = runner.calls[0]
    assert "terminate" in argv or "kill" in argv
    assert "sess-abc123" in argv


# ------------------------------------------------------- command-wrapper seam

class FakeLock:
    """In-memory advisory-lock seam: records acquire/release order per key,
    with no real host-level flock ever taken."""

    def __init__(self):
        self.events: list[tuple[str, str]] = []
        self._held: set[str] = set()

    def acquire(self, key: str) -> None:
        assert key not in self._held, f"lock {key!r} already held — not exclusive"
        self._held.add(key)
        self.events.append(("acquire", key))

    def release(self, key: str) -> None:
        self._held.discard(key)
        self.events.append(("release", key))


def test_command_wrapper_holds_lock_for_the_duration_of_the_run():
    lock = FakeLock()
    runner = FakeRunner(outputs=[_Result(0, "ok", "")])
    wrapper = AdvisoryLockCommandWrapper(lock=lock, runner=runner)

    result = wrapper.run(["pytest", "-k", "port_5433"], lock_key="repo:acme/backend")

    assert result.returncode == 0
    assert lock.events == [
        ("acquire", "repo:acme/backend"),
        ("release", "repo:acme/backend"),
    ]
    assert runner.calls == [["pytest", "-k", "port_5433"]]


def test_command_wrapper_releases_lock_even_when_command_fails():
    lock = FakeLock()
    runner = FakeRunner(outputs=[_Result(1, "", "fail")])
    wrapper = AdvisoryLockCommandWrapper(lock=lock, runner=runner)

    result = wrapper.run(["pytest"], lock_key="repo:acme/backend")

    assert result.returncode == 1
    assert lock.events == [
        ("acquire", "repo:acme/backend"),
        ("release", "repo:acme/backend"),
    ]


def test_command_wrapper_serializes_two_calls_on_the_same_key():
    lock = FakeLock()
    runner = FakeRunner()
    wrapper = AdvisoryLockCommandWrapper(lock=lock, runner=runner)

    wrapper.run(["step-one"], lock_key="repo:acme/backend")
    wrapper.run(["step-two"], lock_key="repo:acme/backend")

    # Each run acquires then releases before the next acquires — never
    # interleaved — so a second contending command never starts mid-lock.
    assert lock.events == [
        ("acquire", "repo:acme/backend"),
        ("release", "repo:acme/backend"),
        ("acquire", "repo:acme/backend"),
        ("release", "repo:acme/backend"),
    ]


# --------------------------------------------------------- manual on-box smoke

@pytest.mark.skipif(
    not os.environ.get("BOX_SERVICE_MANUAL_SMOKE"),
    reason=(
        "manual on-box smoke item (R83): the automated suite above proves the "
        "seam contract with a fake runner; this test exercises the REAL "
        "ClaudeSessionLauncher against the real `claude` CLI once. Run it "
        "explicitly on a box with the CLI installed via "
        "BOX_SERVICE_MANUAL_SMOKE=1 uv run --no-sync pytest "
        "knowledge/serve/tests/test_session_launcher_seam.py -k manual_smoke"
    ),
)
def test_manual_smoke_real_claude_session_lifecycle(tmp_path):
    launcher = ClaudeSessionLauncher()  # real subprocess.run seam, no fake
    session_id = launcher.launch(cwd=str(tmp_path), prompt="echo hello")
    try:
        sessions = launcher.list_sessions()
        assert any(s.id == session_id for s in sessions)
        launcher.resume(session_id, message="are you still there?")
    finally:
        launcher.terminate(session_id)
