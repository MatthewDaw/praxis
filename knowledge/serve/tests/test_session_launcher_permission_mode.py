"""R19: a box-launched build session runs unattended (no TTY to answer a permission
prompt), so ``SessionLauncher.launch`` must invoke the ``claude`` CLI under a
permission mode that never falls back to interactive approval, paired with an
explicit denylist that keeps the session from reaching a cloud instance's own
credential endpoints (the AWS/GCP/Azure IMDS link-local address plus each
provider's credential/token-read CLI surface) — asserted against the named
session-launcher seam (no real background session is ever started)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from knowledge.serve.session_launcher import (
    DENIED_CREDENTIAL_TOOLS,
    PERMISSION_MODE,
    SessionLauncher,
)

#: Interactive permission modes a launched build session must never run under —
#: an unattended session has no TTY to answer either kind of prompt.
_INTERACTIVE_MODES = {"default", "plan"}

#: Substrings any denylisted pattern must be provable against: the shared cloud
#: instance-metadata address, plus one credential/token-read surface per major
#: cloud provider's own CLI.
_CREDENTIAL_SIGNATURES = (
    "169.254.169.254",
    "aws sts get-caller-identity",
    "gcloud auth print-access-token",
    "az account get-access-token",
)


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


def test_launch_runs_under_a_noninteractive_permission_mode():
    runner = FakeRunner(stdout="sess-123\n")
    launcher = SessionLauncher(runner=runner)

    launcher.launch(cwd="/repo/worktree", command="/af-build", name="job-1")

    args = runner.calls[0]["args"]
    assert "--permission-mode" in args
    mode = args[args.index("--permission-mode") + 1]
    assert mode not in _INTERACTIVE_MODES
    assert mode == PERMISSION_MODE


def test_launch_disallows_every_cloud_credential_surface():
    runner = FakeRunner(stdout="sess-123\n")
    launcher = SessionLauncher(runner=runner)

    launcher.launch(cwd="/repo/worktree", command="/af-build", name="job-1")

    args = runner.calls[0]["args"]
    assert "--disallowedTools" in args
    flag_at = args.index("--disallowedTools")
    # Variadic flag: every entry up to the next "--"-prefixed flag (or end) is denied.
    denied = []
    for tok in args[flag_at + 1:]:
        if tok.startswith("--"):
            break
        denied.append(tok)
    assert denied == list(DENIED_CREDENTIAL_TOOLS)
    for signature in _CREDENTIAL_SIGNATURES:
        assert any(signature in pattern for pattern in denied), (
            f"no denylisted pattern covers {signature!r}"
        )


def test_launch_is_allowlist_driven_regardless_of_who_calls_it():
    """The permission mode and denylist are not opt-in per caller — every launch
    of a build session carries them, with no parameter to omit them."""
    runner = FakeRunner(stdout="sess-456\n")
    launcher = SessionLauncher(runner=runner)

    launcher.launch(cwd="/repo/worktree", command="/af-build")

    args = runner.calls[0]["args"]
    assert "--permission-mode" in args
    assert "--disallowedTools" in args
