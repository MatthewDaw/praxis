"""The named session-launcher seam (R13, R21) the box service uses to reach
the built-in Claude Code background daemon.

Every method routes its subprocess call through an injectable ``runner`` —
same call signature as :func:`subprocess.run` — so the box service's launch,
list, resume, and terminate contract is fully assertable against a fake in
tests, with no real background session ever started (the ``session-lifecycle``
check this seam exists to satisfy: "asserted against the named
session-launcher seam with no real background session started, so the
contract is verifiable without a live nested CLI").

``launch`` spawns the build session itself, so it passes an explicit,
credential-sanitized ``env`` (:func:`_sanitized_session_env`) rather than letting the child
inherit the box service's own process environment wholesale (R37): the push credential lives in
AWS Secrets Manager, fetched via the box's own ambient role and handed only to
``github_token``/``box_service_push_auth`` in this process's memory, never through an
environment variable — but the box process may still carry AWS static credentials or a
``GITHUB_TOKEN``-shaped variable of its own (e.g. local dev), and a spawned build session must
never inherit them.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from typing import Any

from knowledge.serve.box_service_models import SessionInfo

#: Same shape as ``subprocess.run`` — the seam a fake runner replaces in tests.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]

#: Environment-variable name prefixes/substrings a build session must never inherit — the AWS
#: credential family (static keys, session/assumed-role tokens, profile selection) and any
#: GitHub-token-shaped variable (the account-wide PAT this same process may hold in memory).
_CREDENTIAL_ENV_MARKERS = ("AWS_", "GITHUB_TOKEN")


def _sanitized_session_env() -> dict[str, str]:
    """A copy of the box service's own process environment with every credential-shaped variable
    removed, so a launched build session's environment carries no service token or cloud
    credential variable (R37) even though the box service's own process may hold one."""
    return {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key for marker in _CREDENTIAL_ENV_MARKERS)
    }


class SessionLauncherError(RuntimeError):
    """Raised when the underlying CLI call fails or returns an unusable
    payload. Never silently swallowed (R17: refuse rather than degrade)."""


class SessionLauncher:
    """Thin wrapper over the ``claude`` CLI's background-session surface."""

    def __init__(self, runner: Runner = subprocess.run, cli: str = "claude") -> None:
        self._runner = runner
        self._cli = cli

    def launch(
        self,
        *,
        cwd: str,
        command: str,
        name: str | None = None,
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Start a ``claude --bg`` session and return its session id.

        ``extra_args`` (e.g. the R14 per-dispatch plugin/mcp-config/settings flags
        from ``dispatch_launch``, or R28's mailbox-relay Stop hook) are appended after
        ``--name``. ``env`` defaults to an explicit, credential-sanitized copy of the box
        service's own environment (:func:`_sanitized_session_env`) rather than the child
        inheriting it wholesale (R37); pass an explicit ``env`` (e.g. R29's resumed-owner
        identity) to override.
        """
        args = [self._cli, "--bg", command]
        if name is not None:
            args += ["--name", name]
        if extra_args:
            args += extra_args
        proc = self._runner(
            args, cwd=cwd, capture_output=True, text=True, check=False,
            env=env if env is not None else _sanitized_session_env(),
        )
        if proc.returncode != 0:
            raise SessionLauncherError(f"launch failed: {proc.stderr.strip()}")
        session_id = proc.stdout.strip()
        if not session_id:
            raise SessionLauncherError("launch produced no session id")
        return session_id

    def list(self) -> list[SessionInfo]:
        """Poll ``claude agents --json`` (R21) for every live session."""
        proc = self._runner(
            [self._cli, "agents", "--json"], capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            raise SessionLauncherError(f"list failed: {proc.stderr.strip()}")
        rows: list[dict[str, Any]] = json.loads(proc.stdout or "[]")
        return [
            SessionInfo(
                session_id=row["session_id"],
                cwd=row["cwd"],
                kind=row["kind"],
                started_at=row["started_at"],
                name=row["name"],
                state=row["state"],
            )
            for row in rows
        ]

    def resume(self, session_id: str) -> bool:
        """Resume a background session; ``True`` iff the CLI reports success."""
        proc = self._runner(
            [self._cli, "agents", "resume", session_id],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0

    def terminate(self, session_id: str) -> bool:
        """Terminate (reap) a background session; ``True`` iff it succeeded."""
        proc = self._runner(
            [self._cli, "agents", "terminate", session_id],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0
