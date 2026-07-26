"""The session-launcher seam (R83).

Every requirement whose behavior can only be observed on a live background
session — launch, list, resume, terminate — is verified against this NAMED
injectable seam rather than a real session. ``ClaudeSessionLauncher`` is the
real implementation, wrapping the Claude Code CLI's background-session
daemon (``claude --bg`` / ``claude agents --json`` / ``claude resume`` /
``claude terminate``); its ``runner`` constructor argument is the injection
point a test replaces with a fake that never spawns a process.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Callable, Protocol


class _RunResult(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., _RunResult]


def _default_runner(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


@dataclass(frozen=True)
class SessionInfo:
    """One row of ``claude agents --json``, normalized to the fields this
    seam's callers rely on."""

    id: str
    cwd: str
    kind: str
    started_at: str
    name: str
    state: str


class SessionLauncher(Protocol):
    """The named seam: launch, list, resume, terminate — nothing else. Any
    requirement whose behavior can only be observed on a live background
    session is asserted against this contract, never against a real
    session."""

    def launch(self, *, cwd: str, prompt: str) -> str: ...

    def list_sessions(self) -> list[SessionInfo]: ...

    def resume(self, session_id: str, *, message: str) -> None: ...

    def terminate(self, session_id: str) -> None: ...


class ClaudeSessionLauncher:
    """Real implementation over the Claude Code CLI's background-session
    daemon. Never shells out to ``tmux`` or any hand-managed process
    supervisor — the daemon owns the session's lifecycle."""

    def __init__(self, *, runner: Runner = _default_runner) -> None:
        self._runner = runner

    def launch(self, *, cwd: str, prompt: str) -> str:
        result = self._runner(["claude", "--bg", "--cwd", cwd, prompt])
        if result.returncode != 0:
            raise RuntimeError(
                f"claude --bg failed (exit {result.returncode}): {result.stderr}"
            )
        return result.stdout.strip()

    def list_sessions(self) -> list[SessionInfo]:
        result = self._runner(["claude", "agents", "--json"])
        if result.returncode != 0:
            raise RuntimeError(
                f"claude agents --json failed (exit {result.returncode}): {result.stderr}"
            )
        rows = json.loads(result.stdout or "[]")
        return [
            SessionInfo(
                id=row["id"],
                cwd=row["cwd"],
                kind=row["kind"],
                started_at=row["startedAt"],
                name=row["name"],
                state=row["state"],
            )
            for row in rows
        ]

    def resume(self, session_id: str, *, message: str) -> None:
        result = self._runner(["claude", "resume", session_id, message])
        if result.returncode != 0:
            raise RuntimeError(
                f"claude resume failed (exit {result.returncode}): {result.stderr}"
            )

    def terminate(self, session_id: str) -> None:
        result = self._runner(["claude", "terminate", session_id])
        if result.returncode != 0:
            raise RuntimeError(
                f"claude terminate failed (exit {result.returncode}): {result.stderr}"
            )
