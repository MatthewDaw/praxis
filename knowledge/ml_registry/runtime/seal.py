"""Seatbelt boundaries around the two untrusted agents: the training arm, and the proposer.

The arm's seal governs the training run — sealed labels unreadable, no egress, writes confined to a
predictions directory. The proposer's profile governs the agent that *writes* the arm: writes
confined to its own arm worktree, the sealed split unreadable, and the harness that will score it
unwritable. Both are proven at launch by a three-legged positive control, and any leg that does not
raise refuses the launch — a profile built from a non-canonical path once blocked nothing while
network-deny still worked, so a profile that looks healthy is not evidence that it is.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Mapping, Protocol, Sequence


class SealError(ValueError):
    """A profile could not be proven, or an arm or proposer violated its contract."""


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        capture_output: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]: ...


def _canonical(path: str | Path) -> Path:
    return Path(os.path.realpath(os.fspath(path)))


def _policy_path(name: str, path: Path, *, directory: bool) -> None:
    """Refuse a policy path that is not canonical, or that does not exist to be canonical about."""
    if path != _canonical(path):
        raise SealError(f"{name} must be canonical (use os.path.realpath)")
    if not (path.is_dir() if directory else path.is_file()):
        raise SealError(f"{name} must be an existing {'directory' if directory else 'file'}")


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _subpath(path: Path) -> str:
    """The path as a quoted Seatbelt literal."""
    return json.dumps(os.fspath(path))


class SandboxProfile(Protocol):
    """A Seatbelt policy plus the probes that prove its denies actually deny."""

    @property
    def text(self) -> str: ...

    @property
    def control_probes(self) -> tuple[tuple[str, str, Path | None], ...]: ...


@dataclass(frozen=True)
class SeatbeltProfile:
    sealed_file: Path
    predictions_dir: Path

    def __post_init__(self) -> None:
        _policy_path("sealed_file", self.sealed_file, directory=False)
        _policy_path("predictions_dir", self.predictions_dir, directory=True)
        if self.predictions_dir == self.sealed_file.parent:
            raise SealError("predictions_dir must not contain the sealed file")

    @classmethod
    def create(
        cls, *, sealed_file: str | Path, predictions_dir: str | Path,
    ) -> SeatbeltProfile:
        """Build the profile only after resolving both policy paths with realpath."""
        return cls(_canonical(sealed_file), _canonical(predictions_dir))

    @property
    def text(self) -> str:
        return "\n".join((
            "(version 1)",
            "(allow default)",
            "(deny network*)",
            f"(deny file-read* (subpath {_subpath(self.sealed_file)}))",
            "(deny file-write*)",
            f"(allow file-write* (subpath {_subpath(self.predictions_dir)}))",
        ))

    @property
    def control_probes(self) -> tuple[tuple[str, str, Path | None], ...]:
        return (
            ("sealed-read", _READ_CONTROL, self.sealed_file),
            ("network-egress", _NETWORK_CONTROL, None),
            ("outside-write", _WRITE_CONTROL, self.predictions_dir.parent / _CONTROL_SCRATCH),
        )


@dataclass(frozen=True)
class ProposerProfile:
    """The confinement of the agent that writes arm code, which the arm's own seal does not cover.

    It keeps the network — it retrieves techniques — but it writes only inside the arm worktree it
    was given, cannot read the sealed split, and cannot edit the harness that will score it. An arm
    that can edit its own scorer has no seal at all.
    """

    arm_worktree: Path
    sealed_file: Path
    scoring_harness: Path

    def __post_init__(self) -> None:
        _policy_path("arm_worktree", self.arm_worktree, directory=True)
        _policy_path("sealed_file", self.sealed_file, directory=False)
        _policy_path("scoring_harness", self.scoring_harness, directory=True)
        if _within(self.sealed_file, self.arm_worktree):
            raise SealError("arm_worktree must not contain the sealed file")
        if _within(self.arm_worktree, self.scoring_harness):
            raise SealError("scoring_harness must not contain the arm worktree")

    @classmethod
    def create(
        cls, *, arm_worktree: str | Path, sealed_file: str | Path, scoring_harness: str | Path,
    ) -> ProposerProfile:
        """Build the profile only after resolving all three policy paths with realpath."""
        return cls(_canonical(arm_worktree), _canonical(sealed_file), _canonical(scoring_harness))

    @property
    def text(self) -> str:
        # The harness deny comes LAST deliberately: Seatbelt takes the last matching rule, and the
        # harness normally sits inside the worktree the rule above it just made writable.
        return "\n".join((
            "(version 1)",
            "(allow default)",
            f"(deny file-read* (subpath {_subpath(self.sealed_file)}))",
            "(deny file-write*)",
            f"(allow file-write* (subpath {_subpath(self.arm_worktree)}))",
            f"(deny file-write* (subpath {_subpath(self.scoring_harness)}))",
        ))

    @property
    def control_probes(self) -> tuple[tuple[str, str, Path | None], ...]:
        return (
            ("sealed-read", _READ_CONTROL, self.sealed_file),
            ("outside-write", _WRITE_CONTROL, self.arm_worktree.parent / _CONTROL_SCRATCH),
            ("harness-write", _WRITE_CONTROL, self.scoring_harness / _CONTROL_SCRATCH),
        )


@dataclass(frozen=True)
class PredictionsArtifact:
    """The arm's complete output; its trusted caller reads this path and scores it outside."""

    path: Path


_READ_CONTROL = """\
import pathlib, sys
try:
    pathlib.Path(sys.argv[1]).read_bytes()
except OSError:
    raise SystemExit(0)
raise SystemExit(10)
"""
_NETWORK_CONTROL = """\
import socket
try:
    socket.create_connection(("1.1.1.1", 53), timeout=.25)
except OSError:
    raise SystemExit(0)
raise SystemExit(10)
"""
_WRITE_CONTROL = """\
import pathlib, sys
try:
    pathlib.Path(sys.argv[1]).write_text("seal-control")
except OSError:
    raise SystemExit(0)
raise SystemExit(10)
"""
_CONTROL_SCRATCH = ".praxis-seal-positive-control"


def _environment(predictions_file: Path | None = None) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR"}
    }
    if predictions_file is not None:
        environment["PRAXIS_PREDICTIONS_FILE"] = os.fspath(predictions_file)
    return environment


def _sandboxed(
    profile: SandboxProfile,
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    runner: CommandRunner,
) -> subprocess.CompletedProcess[str]:
    return runner(
        ["/usr/bin/sandbox-exec", "-p", profile.text, "--", *command],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
    )


def _positive_control(
    profile: SandboxProfile, *, cwd: Path, runner: CommandRunner,
) -> None:
    """Prove every leg of the profile denies what it claims to, or refuse the launch."""
    probes = profile.control_probes
    scratch = [
        argument for _, program, argument in probes
        if program is _WRITE_CONTROL and argument is not None
    ]
    for path in scratch:
        path.unlink(missing_ok=True)
    try:
        for name, program, argument in probes:
            command = [sys.executable, "-c", program]
            if argument is not None:
                command.append(os.fspath(argument))
            command.append(name)
            result = _sandboxed(
                profile, command, cwd=cwd, environment=_environment(), runner=runner,
            )
            if result.returncode != 0:
                raise SealError(
                    f"{name} positive control failed; campaign launch refused "
                    f"(sandbox exit {result.returncode})"
                )
    finally:
        for path in scratch:
            path.unlink(missing_ok=True)


def _require_argv(command: Sequence[str], role: str) -> None:
    if not command or not all(isinstance(item, str) and item for item in command):
        raise SealError(f"{role} command must be a non-empty argv sequence")


def launch_sealed_arm(
    profile: SeatbeltProfile,
    command: Sequence[str],
    *,
    predictions_file: str,
    cwd: str | Path,
    runner: CommandRunner = subprocess.run,
) -> PredictionsArtifact:
    """Prove all three denies, then run an arm whose sole artifact is predictions."""
    _require_argv(command, "arm")
    if not predictions_file or Path(predictions_file).name != predictions_file:
        raise SealError("predictions_file must be a safe single filename")
    existing = list(profile.predictions_dir.iterdir())
    if existing:
        raise SealError("predictions_dir must be empty before an arm starts")
    workdir = _canonical(cwd)
    if not workdir.is_dir():
        raise SealError("arm working directory must exist")

    _positive_control(profile, cwd=workdir, runner=runner)
    output = profile.predictions_dir / predictions_file
    result = _sandboxed(
        profile, command, cwd=workdir, environment=_environment(output), runner=runner,
    )
    if result.returncode != 0:
        raise SealError(f"arm exited {result.returncode}")
    emitted = list(profile.predictions_dir.rglob("*"))
    if emitted != [output] or not output.is_file():
        unexpected = sorted(os.fspath(path.relative_to(profile.predictions_dir)) for path in emitted)
        raise SealError(f"arm emitted unexpected output; expected only {predictions_file}: {unexpected}")
    return PredictionsArtifact(output)


def launch_proposer(
    profile: ProposerProfile,
    command: Sequence[str],
    *,
    runner: CommandRunner = subprocess.run,
) -> None:
    """Prove all three proposer denies at campaign start, then run the proposer confined by them.

    Same refuse-the-launch rule as the arm's seal: a leg that does not raise means the profile is
    not enforcing what it says, so the campaign never starts.
    """
    _require_argv(command, "proposer")
    _positive_control(profile, cwd=profile.arm_worktree, runner=runner)
    result = _sandboxed(
        profile, command, cwd=profile.arm_worktree, environment=_environment(), runner=runner,
    )
    if result.returncode != 0:
        raise SealError(f"proposer exited {result.returncode}")
