"""af-clean's validation-and-remediation step (R31) — decomposable and standalone.

This step is the eight-phase gate af-clean runs over a candidate diff before it can land: full
test suite, typecheck, lint, an AST reference sweep, a reachability re-check, the query-resolved
building-validation lane (when present), a bounded remediation loop, and an advisory coverage
report. It is deliberately a PLAIN FUNCTION over explicit inputs — a repo path, a command runner,
and a handful of optional callbacks — so it is callable standalone (a unit test, a CLI, a
different caller) with NO af-clean run in progress and no shared/global/module-level state to
coordinate with. Nothing here reaches into Praxis or a build marker; the caller (af-clean) is the
only thing that knows about runs.

Command discovery prefers the repo's OWN configuration/package-manager scripts (package.json
``scripts``, a native Python tool config) over a generic recipe runner (``make``/``just``): a
``just test`` recipe may not exist, may be stale, or may wrap only a subset of what CI actually
runs, so it is used only as a last resort when nothing else is discoverable.

A failing phase feeds the bounded remediation loop; if the loop exhausts its ``iteration_cap``
with failures still outstanding, the step ESCALATES (``overall_status == ESCALATED``) rather than
silently reporting success — an exhausted cap is a stop, never a pass.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# The eight named phases, in the order the step must run them.
PHASE_TEST_SUITE = "full_test_suite"
PHASE_TYPECHECK = "typecheck"
PHASE_LINT = "lint"
PHASE_AST_SWEEP = "ast_reference_sweep"
PHASE_REACHABILITY = "reachability_recheck"
PHASE_BUILDING_VALIDATION = "building_validation_lane"
PHASE_REMEDIATION = "remediation_loop"
PHASE_COVERAGE_REPORT = "advisory_coverage_report"

PHASES = (
    PHASE_TEST_SUITE,
    PHASE_TYPECHECK,
    PHASE_LINT,
    PHASE_AST_SWEEP,
    PHASE_REACHABILITY,
    PHASE_BUILDING_VALIDATION,
    PHASE_REMEDIATION,
    PHASE_COVERAGE_REPORT,
)

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
ESCALATED = "escalated"

# A runner takes an argv list and a cwd, and returns anything exposing ``.returncode``,
# ``.stdout``, ``.stderr`` (a ``subprocess.CompletedProcess`` in production; a stub in tests).
CommandRunner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


def default_runner(argv: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    """The real command runner: a subprocess against the target repo."""
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=1800)


@dataclass
class PhaseResult:
    """One phase's (or one building-validation check's) outcome."""

    name: str
    status: str
    command: str = ""
    detail: str = ""


@dataclass
class ValidateRemediateReport:
    """The step's full output: exactly one :class:`PhaseResult` per named phase, in order, plus
    the individual building-validation check results and any advisory warnings."""

    phases: list[PhaseResult] = field(default_factory=list)
    building_validation_results: list[PhaseResult] = field(default_factory=list)
    overall_status: str = PASSED
    warnings: list[str] = field(default_factory=list)
    remediation_attempts: int = 0

    @property
    def passed(self) -> bool:
        return self.overall_status == PASSED


# --------------------------------------------------------------------------- command discovery

def discover_commands(repo_path: "str | Path") -> dict[str, Optional[str]]:
    """Discover the repo's REAL test/typecheck/lint commands.

    Preference order per capability: (1) a package.json ``scripts`` entry, (2) native Python tool
    configuration (pytest/ruff/mypy config files), (3) a ``Makefile``/``justfile`` target — a
    generic recipe runner is the LAST resort, never the default assumption, because a repo's own
    package-manager scripts or tool config is the ground truth for what CI actually runs. A
    capability with nothing discoverable stays ``None`` rather than a fabricated guess.
    """
    repo = Path(repo_path)
    commands: dict[str, Optional[str]] = {"test": None, "typecheck": None, "lint": None}

    pkg_json = repo / "package.json"
    if pkg_json.is_file():
        try:
            scripts = (json.loads(pkg_json.read_text()) or {}).get("scripts") or {}
        except (OSError, json.JSONDecodeError):
            scripts = {}
        for cap, names in (
            ("test", ("test",)),
            ("typecheck", ("typecheck", "type-check")),
            ("lint", ("lint",)),
        ):
            for name in names:
                if name in scripts:
                    commands[cap] = f"npm run {name}"
                    break

    def _read(*names: str) -> str:
        text = ""
        for name in names:
            p = repo / name
            if p.is_file():
                text += p.read_text(errors="ignore")
        return text

    py_config_text = _read("pyproject.toml", "pytest.ini", "setup.cfg")
    if commands["test"] is None and (
        "pytest" in py_config_text or (repo / "pytest.ini").is_file()
    ):
        commands["test"] = "python -m pytest -q"
    if commands["lint"] is None and (
        "[tool.ruff]" in py_config_text
        or (repo / "ruff.toml").is_file()
        or (repo / ".ruff.toml").is_file()
    ):
        commands["lint"] = "ruff check ."
    if commands["typecheck"] is None and (
        "[tool.mypy]" in py_config_text or (repo / "mypy.ini").is_file()
    ):
        commands["typecheck"] = "mypy ."
    if commands["lint"] is None and (
        (repo / ".eslintrc.json").is_file() or (repo / ".eslintrc.js").is_file()
    ):
        commands["lint"] = "npx eslint ."
    if commands["typecheck"] is None and (repo / "tsconfig.json").is_file():
        commands["typecheck"] = "npx tsc --noEmit"

    for cap in ("test", "typecheck", "lint"):
        if commands[cap] is not None:
            continue
        makefile_text = _read("Makefile")
        if makefile_text and re.search(rf"(?m)^{cap}\s*:", makefile_text):
            commands[cap] = f"make {cap}"
            continue
        justfile_text = _read("justfile")
        if justfile_text and re.search(rf"(?m)^{cap}\b", justfile_text):
            commands[cap] = f"just {cap}"

    return commands


# --------------------------------------------------------------------------- coverage floor

_FAIL_UNDER_RE = re.compile(r"fail[_-]under\s*=\s*([\d.]+)")


def detect_coverage_floor(repo_path: "str | Path") -> Optional[str]:
    """A human-readable warning iff the target repo enforces a CI coverage floor that a
    deletion-heavy change could trip, else ``None``. Best-effort / advisory only — never raises,
    never blocks."""
    repo = Path(repo_path)
    for name in ("pyproject.toml", ".coveragerc", "setup.cfg"):
        p = repo / name
        if not p.is_file():
            continue
        m = _FAIL_UNDER_RE.search(p.read_text(errors="ignore"))
        if m:
            return f"{name} enforces a coverage floor (fail_under={m.group(1)}) that deletions could trip"
    pkg = repo / "package.json"
    if pkg.is_file() and "coverageThreshold" in pkg.read_text(errors="ignore"):
        return "package.json enforces a jest coverageThreshold that deletions could trip"
    if (repo / "codecov.yml").is_file():
        return "codecov.yml is present — deletions may trip an external coverage gate"
    return None


# --------------------------------------------------------------------------- the step

def run_validation_and_remediation(
    repo_path: "str | Path",
    *,
    runner: CommandRunner = default_runner,
    commands: Optional[dict[str, Optional[str]]] = None,
    building_validation_checks: Optional[list[dict]] = None,
    ast_reference_sweep: Optional[Callable[[Path], PhaseResult]] = None,
    reachability_recheck: Optional[Callable[[Path], PhaseResult]] = None,
    remediate: Optional[Callable[[str, PhaseResult], bool]] = None,
    iteration_cap: int = 3,
) -> ValidateRemediateReport:
    """Run the eight-phase validation-and-remediation step.

    Standalone: every input is explicit (repo path, runner, checks, callbacks) — there is no
    af-clean run marker or module-level state to coordinate with, so this is safely callable with
    no af-clean run in progress. Phases run in the fixed :data:`PHASES` order. The
    building-validation lane runs only ``when present`` (``building_validation_checks`` non-empty);
    an empty/``None`` list SKIPS the lane rather than treating absence as failure. A failing phase
    feeds the bounded remediation loop (at most ``iteration_cap`` passes); if failures remain once
    the cap is exhausted, the step ESCALATES rather than reporting a pass.
    """
    repo = Path(repo_path)
    commands = commands if commands is not None else discover_commands(repo)
    report = ValidateRemediateReport()

    def _run_named(name: str, cmd: Optional[str]) -> None:
        if not cmd:
            report.phases.append(PhaseResult(name, SKIPPED, detail="no command discovered"))
            return
        proc = runner(shlex.split(cmd), repo)
        status = PASSED if proc.returncode == 0 else FAILED
        report.phases.append(
            PhaseResult(name, status, command=cmd, detail=(proc.stdout or "") + (proc.stderr or ""))
        )

    _run_named(PHASE_TEST_SUITE, commands.get("test"))
    _run_named(PHASE_TYPECHECK, commands.get("typecheck"))
    _run_named(PHASE_LINT, commands.get("lint"))

    for phase_name, hook in (
        (PHASE_AST_SWEEP, ast_reference_sweep),
        (PHASE_REACHABILITY, reachability_recheck),
    ):
        if hook is None:
            report.phases.append(PhaseResult(phase_name, SKIPPED, detail="no sweep provided"))
        else:
            report.phases.append(hook(repo))

    if building_validation_checks:
        lane_status = PASSED
        for chk in building_validation_checks:
            meta = chk.get("meta") or {}
            cmd = meta.get("run") or chk.get("run")
            check_id = str(meta.get("check_id") or chk.get("id") or "check")
            if not cmd:
                # A resolved check with no ``run`` command CANNOT be verified. Skipping it silently
                # (the prior behavior) let the lane report PASSED while a check nobody ran counted
                # as satisfied -- unrunnable rendered as passed. Fail the lane instead: an
                # un-executable gate is a defect in the check, surfaced, never a free pass.
                lane_status = FAILED
                report.building_validation_results.append(
                    PhaseResult(check_id, FAILED,
                                detail="check has no `run` command — cannot be executed, so it "
                                       "cannot be counted as passed")
                )
                report.warnings.append(
                    f"building-validation check {check_id!r} has no run command and was not verified")
                continue
            proc = runner(shlex.split(cmd), repo)
            status = PASSED if proc.returncode == 0 else FAILED
            if status == FAILED:
                lane_status = FAILED
            report.building_validation_results.append(
                PhaseResult(check_id, status, command=cmd, detail=(proc.stdout or "") + (proc.stderr or ""))
            )
        report.phases.append(PhaseResult(PHASE_BUILDING_VALIDATION, lane_status))
    else:
        report.phases.append(
            PhaseResult(PHASE_BUILDING_VALIDATION, SKIPPED, detail="no building-validation checks resolved")
        )

    def _failing() -> list[PhaseResult]:
        return [p for p in report.phases if p.status == FAILED]

    attempts = 0
    while _failing() and attempts < iteration_cap:
        if remediate is None:
            break
        attempts += 1
        for pr in list(_failing()):
            if not remediate(pr.name, pr):
                continue
            if not pr.command:
                continue
            idx = report.phases.index(pr)
            proc = runner(shlex.split(pr.command), repo)
            new_status = PASSED if proc.returncode == 0 else FAILED
            report.phases[idx] = PhaseResult(
                pr.name, new_status, command=pr.command, detail=(proc.stdout or "") + (proc.stderr or "")
            )
    report.remediation_attempts = attempts

    still_failing = _failing()
    report.phases.append(
        PhaseResult(
            PHASE_REMEDIATION,
            PASSED if not still_failing else FAILED,
            detail=f"{attempts} attempt(s); still failing: {[p.name for p in still_failing]}"
            if still_failing
            else f"{attempts} attempt(s)",
        )
    )
    # Cap exhaustion (or no remediation callback at all) with failures outstanding must ESCALATE —
    # never silently report a pass.
    report.overall_status = ESCALATED if still_failing else PASSED

    floor_warning = detect_coverage_floor(repo)
    if floor_warning:
        report.warnings.append(floor_warning)
    report.phases.append(
        PhaseResult(
            PHASE_COVERAGE_REPORT,
            PASSED,  # advisory only — never gates the step
            detail="; ".join(report.warnings) if report.warnings else "no coverage floor detected",
        )
    )

    return report
