"""Toolchain detection for af-clean (R1).

af-clean detects the target repository's languages, package managers, test runners, linters,
and type checkers PER INVOCATION rather than assuming any toolchain -- nothing about the
environment is hardcoded, since af-clean's primary use is arbitrary repositories it has never
seen before.

This module also probes zero-install availability of the five census detectors named by the
requirement (Vulture, Knip, jscpd, radon, semgrep) without ever mutating the target repo's
manifests or lockfiles (D7/B2 posture): presence is checked via ``shutil.which`` first (an
already-installed binary), then via an ephemeral ``uvx``/``npx -y`` invocation whose cache lives
outside the target repo entirely -- matching the existing ``uvx ruff@0.15.20`` CI pattern in
this repo. A failed or offline probe is reported as "absent", never guessed as present.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

# Pinned per D7 ("uvx <tool>@<pinned>" / "npx -y <tool>@<pinned>"). These are the FIVE
# detectors the ticket's acceptance condition names by name; the dict is the single source
# of truth for "which detectors does af-clean report on" -- nothing else is hardcoded.
DETECTOR_PINS: dict[str, str] = {
    "vulture": "2.14",
    "radon": "6.0.1",
    "semgrep": "1.99.0",
    "knip": "5.62.0",
    "jscpd": "4.0.5",
}

# Which zero-install runtime hosts each detector's ephemeral invocation.
_DETECTOR_RUNTIME: dict[str, str] = {
    "vulture": "uvx",
    "radon": "uvx",
    "semgrep": "uvx",
    "knip": "npx",
    "jscpd": "npx",
}

_PROBE_TIMEOUT_S = 5


@dataclass
class LanguageToolchain:
    """One detected language's toolchain, with the file evidence that produced each field."""

    language: str
    package_manager: Optional[str] = None
    test_runner: Optional[str] = None
    linter: Optional[str] = None
    type_checker: Optional[str] = None
    evidence: list[str] = field(default_factory=list)


@dataclass
class ToolchainReport:
    repo_root: str
    languages: list[LanguageToolchain]
    detectors_present: dict[str, bool]

    def detectors_absent(self) -> list[str]:
        return sorted(name for name, present in self.detectors_present.items() if not present)

    def detectors_installed(self) -> list[str]:
        return sorted(name for name, present in self.detectors_present.items() if present)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _load_json(path: Path) -> dict:
    raw = _read_text(path)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _detect_python(root: Path) -> Optional[LanguageToolchain]:
    pyproject = root / "pyproject.toml"
    requirements = sorted(root.glob("requirements*.txt"))
    setup_py = root / "setup.py"
    if not (pyproject.exists() or requirements or setup_py.exists()):
        return None

    evidence: list[str] = []
    text = _read_text(pyproject)
    if pyproject.exists():
        evidence.append("pyproject.toml")
    evidence.extend(r.name for r in requirements)
    if setup_py.exists():
        evidence.append("setup.py")

    package_manager: Optional[str] = None
    if (root / "uv.lock").exists():
        package_manager = "uv"
        evidence.append("uv.lock")
    elif (root / "poetry.lock").exists():
        package_manager = "poetry"
        evidence.append("poetry.lock")
    elif (root / "Pipfile.lock").exists():
        package_manager = "pipenv"
        evidence.append("Pipfile.lock")
    elif requirements:
        package_manager = "pip"

    test_runner: Optional[str] = None
    if (root / "pytest.ini").exists() or (root / "conftest.py").exists():
        test_runner = "pytest"
        evidence.append("pytest.ini/conftest.py")
    elif "pytest" in text or any("pytest" in _read_text(r) for r in requirements):
        test_runner = "pytest"
        evidence.append("pytest dependency")

    linter: Optional[str] = None
    if "[tool.ruff]" in text or (root / "ruff.toml").exists() or (root / ".ruff.toml").exists():
        linter = "ruff"
        evidence.append("[tool.ruff]/ruff.toml")
    elif (root / ".flake8").exists() or "flake8" in text:
        linter = "flake8"
        evidence.append(".flake8/flake8 dependency")

    type_checker: Optional[str] = None
    if "[tool.mypy]" in text or (root / "mypy.ini").exists():
        type_checker = "mypy"
        evidence.append("[tool.mypy]/mypy.ini")
    elif "pyright" in text or (root / "pyrightconfig.json").exists():
        type_checker = "pyright"
        evidence.append("pyright dependency/pyrightconfig.json")

    return LanguageToolchain("python", package_manager, test_runner, linter, type_checker, evidence)


def _detect_javascript(root: Path) -> Optional[LanguageToolchain]:
    package_json = root / "package.json"
    if not package_json.exists():
        return None

    data = _load_json(package_json)
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    scripts = data.get("scripts", {})
    evidence = ["package.json"]

    has_tsconfig = (root / "tsconfig.json").exists()
    language = "typescript" if ("typescript" in deps or has_tsconfig) else "javascript"
    if has_tsconfig:
        evidence.append("tsconfig.json")

    package_manager: Optional[str] = None
    if (root / "pnpm-lock.yaml").exists():
        package_manager = "pnpm"
        evidence.append("pnpm-lock.yaml")
    elif (root / "yarn.lock").exists():
        package_manager = "yarn"
        evidence.append("yarn.lock")
    elif (root / "package-lock.json").exists():
        package_manager = "npm"
        evidence.append("package-lock.json")

    def _first_present(*names: str) -> Optional[str]:
        for name in names:
            if name in deps:
                return name
        for cmd in scripts.values():
            for name in names:
                if name in cmd:
                    return name
        return None

    test_runner = _first_present("vitest", "jest", "mocha", "ava")
    if test_runner:
        evidence.append(f"{test_runner} dependency/script")

    linter = _first_present("eslint")
    if linter:
        evidence.append("eslint dependency")

    type_checker = "tsc" if language == "typescript" else _first_present("flow-bin")
    if type_checker:
        evidence.append(f"{type_checker}")

    return LanguageToolchain(language, package_manager, test_runner, linter, type_checker, evidence)


def _probe_detector(name: str) -> bool:
    """True iff ``name`` is invocable zero-install -- found on PATH, or resolvable via an
    ephemeral ``uvx``/``npx -y`` run -- WITHOUT ever writing into the target repo. Both ``uvx``
    and ``npx`` cache outside the invoking repo (their own tool caches), so a ``--version`` probe
    never touches the target repo's manifests or lockfiles even when it triggers a real
    ephemeral install. An offline or failing probe is reported as absent, never assumed present.
    """
    if shutil.which(name):
        return True

    runtime = _DETECTOR_RUNTIME.get(name)
    pin = DETECTOR_PINS.get(name)
    if runtime is None or pin is None or shutil.which(runtime) is None:
        return False

    if runtime == "uvx":
        cmd = ["uvx", f"{name}@{pin}", "--version"]
    else:
        cmd = ["npx", "-y", f"{name}@{pin}", "--version"]

    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=_PROBE_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def detect_toolchain(repo_root: Union[str, Path], *, probe_detectors: bool = True) -> ToolchainReport:
    """Detect the toolchain of ``repo_root`` fresh, per invocation.

    ``probe_detectors=True`` (the default, matching a real "full run") additionally attempts a
    zero-install ephemeral probe (``uvx``/``npx -y``) for a detector not already on PATH; pass
    ``False`` for a PATH-only check (fast, fully offline, no subprocess spawned).
    """
    root = Path(repo_root)
    languages = [
        lang for lang in (_detect_python(root), _detect_javascript(root)) if lang is not None
    ]
    if probe_detectors:
        detectors_present = {name: _probe_detector(name) for name in DETECTOR_PINS}
    else:
        detectors_present = {name: shutil.which(name) is not None for name in DETECTOR_PINS}
    return ToolchainReport(repo_root=str(root), languages=languages, detectors_present=detectors_present)


def format_report(report: ToolchainReport) -> str:
    lines = [f"Toolchain report for {report.repo_root}"]
    if not report.languages:
        lines.append("  languages: none detected")
    for lang in report.languages:
        lines.append(
            f"  {lang.language}: package_manager={lang.package_manager or 'unknown'} "
            f"test_runner={lang.test_runner or 'none'} linter={lang.linter or 'none'} "
            f"type_checker={lang.type_checker or 'none'}"
        )
        if lang.evidence:
            lines.append(f"    evidence: {', '.join(lang.evidence)}")

    present = report.detectors_installed()
    absent = report.detectors_absent()
    lines.append(f"  detectors present: {', '.join(present) if present else 'none'}")
    lines.append(f"  detectors absent: {', '.join(absent) if absent else 'none'}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    import sys

    args = argv if argv is not None else sys.argv[1:]
    repo_root = args[0] if args else "."
    report = detect_toolchain(repo_root)
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
