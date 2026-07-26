"""Startup pin-and-probe preflight (R16, R17).

The box service pins the Claude Code CLI version it was validated against
(:data:`PINNED_CLI_VERSION`) and, on startup, probes every capability it
relies on: background launch, the session listing's fields and state
vocabulary, per-dispatch hook injection, the terminal event, and resume (in
that order — see :data:`CAPABILITY_NAMES`). It refuses to claim any job when
the installed CLI doesn't match the pin, or when any probe fails, rather than
degrading silently — the same reasoning the plan gives for rejecting
transcript parsing applies here: an internal CLI surface can change without
notice, so it must be checked, not assumed.

This module is pure decision logic — :func:`run_preflight` takes the
installed version string and a dict of already-resolved capability probes,
so the refusal/report contract (R17's acceptance condition) is assertable
without a live nested CLI. :func:`build_default_probes` is the separate,
concrete wiring of those probes onto a real
:class:`~knowledge.serve.session_launcher.SessionLauncher`, itself asserted
against that launcher's injectable-runner seam.
"""

from __future__ import annotations

from collections.abc import Callable

from dataclasses import dataclass

from knowledge.serve.session_launcher import SessionLauncher, SessionLauncherError

#: The Claude Code CLI version this box service was validated against (R16).
#: Bump this deliberately, in the same change that re-validates the box
#: service against the new CLI — never silently.
PINNED_CLI_VERSION = "2.0.0"

#: Every relied-upon capability probed at startup (R17), in the plan's own
#: order. Probes run in this order so ``failed_probe`` is deterministic when
#: more than one capability is broken.
CAPABILITY_NAMES: tuple[str, ...] = (
    "background_launch",
    "session_listing_schema",
    "hook_injection",
    "terminal_event",
    "resume",
)

#: The session-state vocabulary this pin was validated against (part of the
#: "session listing's fields and state vocabulary" probe). A session
#: reporting a state outside this set is exactly the silent capability
#: regression R17 exists to catch.
KNOWN_SESSION_STATES: frozenset[str] = frozenset(
    {"running", "idle", "waiting_for_input", "completed", "failed"}
)

#: A capability probe takes no arguments and returns whether the capability
#: is present; it must never raise (a raising probe is treated as a failure,
#: never an unhandled error — see ``run_preflight``).
CapabilityProbe = Callable[[], bool]

#: Sentinel ``failed_probe`` used for a version mismatch, so the acceptance
#: condition's "the specific failed probe" is always populated on refusal.
VERSION_MISMATCH = "cli_version_mismatch"


@dataclass(frozen=True)
class PreflightResult:
    """The outcome of one preflight pass (R17)."""

    ok: bool
    pinned_version: str
    installed_version: str
    failed_probe: str | None = None

    def report(self) -> str:
        """Human/machine-readable report: pinned version, installed
        version, and (on refusal) the specific failed probe."""
        if self.ok:
            return (
                f"preflight ok: installed={self.installed_version} "
                f"matches pinned={self.pinned_version}, all probes passed"
            )
        return (
            f"preflight failed: '{self.failed_probe}' "
            f"(pinned={self.pinned_version}, installed={self.installed_version})"
        )


class PreflightError(RuntimeError):
    """Raised by :func:`require_claimable` when the preflight is not clean —
    the box service must refuse to claim any job (R17: fail loud, never
    degrade silently)."""

    def __init__(self, result: PreflightResult) -> None:
        super().__init__(result.report())
        self.result = result


def run_preflight(
    installed_version: str,
    probes: dict[str, CapabilityProbe],
    *,
    pinned_version: str = PINNED_CLI_VERSION,
) -> PreflightResult:
    """Run the version pin check, then every capability probe in
    :data:`CAPABILITY_NAMES` order, stopping at the first failure.

    The version check runs before any probe — an installed CLI that already
    fails the pin is not trustworthy signal for its own capability probes.
    ``probes`` must supply a callable for every name in ``CAPABILITY_NAMES``;
    a missing probe is itself a startup wiring defect, so this raises
    ``KeyError`` rather than silently skipping it.
    """
    missing = [name for name in CAPABILITY_NAMES if name not in probes]
    if missing:
        raise KeyError(f"missing capability probe(s): {missing}")

    if installed_version != pinned_version:
        return PreflightResult(
            ok=False,
            pinned_version=pinned_version,
            installed_version=installed_version,
            failed_probe=VERSION_MISMATCH,
        )

    for name in CAPABILITY_NAMES:
        try:
            passed = bool(probes[name]())
        except Exception:
            passed = False
        if not passed:
            return PreflightResult(
                ok=False,
                pinned_version=pinned_version,
                installed_version=installed_version,
                failed_probe=name,
            )

    return PreflightResult(
        ok=True, pinned_version=pinned_version, installed_version=installed_version
    )


def require_claimable(result: PreflightResult) -> None:
    """Raise :class:`PreflightError` unless ``result.ok`` — the single call
    site the box service's claim path calls before claiming any job."""
    if not result.ok:
        raise PreflightError(result)


def build_default_probes(launcher: SessionLauncher) -> dict[str, CapabilityProbe]:
    """Wire the five named capability probes onto a real
    :class:`SessionLauncher`. Each probe swallows
    :class:`SessionLauncherError` as a failure signal rather than letting it
    propagate, matching the contract that a probe returns bool, never raises."""

    def _help(*args: str) -> str:
        proc = launcher._runner(  # noqa: SLF001 - the same injectable seam SessionLauncher itself uses
            [launcher._cli, *args], capture_output=True, text=True, check=False
        )
        return proc.stdout or ""

    def background_launch() -> bool:
        return "--bg" in _help("--help")

    def hook_injection() -> bool:
        return "--hooks" in _help("--help")

    def terminal_event() -> bool:
        return "terminal" in _help("agents", "--help")

    def resume() -> bool:
        return "resume" in _help("agents", "--help")

    def session_listing_schema() -> bool:
        try:
            sessions = launcher.list()
        except SessionLauncherError:
            return False
        return all(session.state in KNOWN_SESSION_STATES for session in sessions)

    return {
        "background_launch": background_launch,
        "session_listing_schema": session_listing_schema,
        "hook_injection": hook_injection,
        "terminal_event": terminal_event,
        "resume": resume,
    }
