"""Seatbelt boundary between an untrusted training arm and trusted scoring."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Mapping, Protocol, Sequence


class SealError(ValueError):
    """The seal could not be proven or an arm violated its output contract."""


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


@dataclass(frozen=True)
class SeatbeltProfile:
    sealed_file: Path
    predictions_dir: Path

    def __post_init__(self) -> None:
        for name, path in (
            ("sealed_file", self.sealed_file),
            ("predictions_dir", self.predictions_dir),
        ):
            if path != _canonical(path):
                raise SealError(f"{name} must be canonical (use os.path.realpath)")
        if not self.sealed_file.is_file():
            raise SealError("sealed_file must be an existing file")
        if not self.predictions_dir.is_dir():
            raise SealError("predictions_dir must be an existing directory")
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
        sealed = json.dumps(os.fspath(self.sealed_file))
        predictions = json.dumps(os.fspath(self.predictions_dir))
        return "\n".join((
            "(version 1)",
            "(allow default)",
            "(deny network*)",
            f"(deny file-read* (subpath {sealed}))",
            "(deny file-write*)",
            f"(allow file-write* (subpath {predictions}))",
        ))


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


def _environment(predictions_file: Path | None = None) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR"}
    }
    if predictions_file is not None:
        environment["PRAXIS_PREDICTIONS_FILE"] = os.fspath(predictions_file)
    return environment


def _sandboxed(
    profile: SeatbeltProfile,
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
    profile: SeatbeltProfile, *, cwd: Path, runner: CommandRunner,
) -> None:
    outside_write = _canonical(profile.predictions_dir.parent / ".praxis-seal-positive-control")
    outside_write.unlink(missing_ok=True)
    probes = (
        ("sealed-read", _READ_CONTROL, profile.sealed_file),
        ("network-egress", _NETWORK_CONTROL, None),
        ("outside-write", _WRITE_CONTROL, outside_write),
    )
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
        outside_write.unlink(missing_ok=True)


def launch_sealed_arm(
    profile: SeatbeltProfile,
    command: Sequence[str],
    *,
    predictions_file: str,
    cwd: str | Path,
    runner: CommandRunner = subprocess.run,
) -> PredictionsArtifact:
    """Prove all three denies, then run an arm whose sole artifact is predictions."""
    if not command or not all(isinstance(item, str) and item for item in command):
        raise SealError("arm command must be a non-empty argv sequence")
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
