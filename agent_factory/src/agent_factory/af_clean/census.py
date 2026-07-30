"""Deterministic detector census -- the LLM-judgment allocation function (R8).

af-clean runs the deterministic detector census repo-wide FIRST and uses per-file slop
density as the allocation function for LLM judgment: only files carrying at least one
detector finding, or a density score above the stated threshold, are sent to the model --
never the whole repo (B7). It also publishes the B8 instrument x pattern matrix: every
declared slop pattern names its deterministic instrument, or the literal marker
``UNINSTRUMENTED_MARKER`` when no detector exists for it (comment terseness,
single-responsibility, same-job identity -- per the af-clean requirements doc).

Detector runners degrade gracefully: an absent/failing detector contributes zero findings
rather than raising, so the census never crashes on a repo missing some of the five tools.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Optional

from .toolchain import DETECTOR_PINS

SOURCE_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx"}
EXCLUDED_DIR_NAMES = {
    "node_modules", ".venv", "venv", "__pycache__", ".git",
    "dist", "build", ".mypy_cache", ".pytest_cache",
}

# A file with >=1 detector finding always joins the judged set regardless of this threshold;
# the threshold only widens the set to files whose density score alone crosses it.
DEFAULT_DENSITY_THRESHOLD = 0.02

# The B8 instrument x pattern matrix: each slop pattern names the deterministic detector
# instrument that measures it (from R1's DETECTOR_PINS), or None when no detector exists for
# it at all -- comment terseness, single-responsibility, and same-job identity are, by the
# requirements doc, judgment-only patterns with no possible deterministic measurement.
PATTERN_INSTRUMENTS: dict[str, Optional[str]] = {
    "dead_code": "vulture",
    "unused_export": "knip",
    "duplication": "jscpd",
    "complexity": "radon",
    "security_pattern": "semgrep",
    "comment_terseness": None,
    "single_responsibility": None,
    "same_job_identity": None,
}

UNINSTRUMENTED_MARKER = "uninstrumented - judgment"

DetectorRunner = Callable[[str, list], dict]


@dataclass
class FileCensus:
    """One file's per-detector finding count and derived slop-density score."""

    path: str
    finding_count: int = 0
    density_score: float = 0.0


@dataclass
class CensusReport:
    total_source_file_count: int
    judged_file_count: int
    judged_files: list[str]
    matrix: dict[str, str]
    density_threshold: float
    per_file: list[FileCensus] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_source_file_count": self.total_source_file_count,
            "judged_file_count": self.judged_file_count,
            "judged_files": list(self.judged_files),
            "matrix": dict(self.matrix),
            "density_threshold": self.density_threshold,
        }


def instrument_matrix() -> dict[str, str]:
    """The B8 instrument x pattern matrix, structural and run-independent: every declared
    slop pattern's instrument name, or ``UNINSTRUMENTED_MARKER`` when no detector exists.

    Every non-``None`` instrument must be one of R1's pinned census detectors -- a matrix
    entry naming an instrument this module cannot actually run would be a silent lie.
    """
    for instrument in PATTERN_INSTRUMENTS.values():
        assert instrument is None or instrument in DETECTOR_PINS, (
            f"matrix names unpinned instrument {instrument!r}"
        )
    return {
        pattern: (instrument or UNINSTRUMENTED_MARKER)
        for pattern, instrument in PATTERN_INSTRUMENTS.items()
    }


def discover_source_files(repo_root: str) -> list[str]:
    """Every source file under ``repo_root`` with a tracked extension, excluding vendored,
    build, and cache directories so the total count reflects real repo source, not deps."""
    out = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [
            d for d in dirnames if d not in EXCLUDED_DIR_NAMES and not d.startswith(".")
        ]
        for name in filenames:
            if os.path.splitext(name)[1] in SOURCE_EXTENSIONS:
                out.append(os.path.relpath(os.path.join(dirpath, name), repo_root))
    return sorted(out)


def _vulture_findings(repo_root: str, files: list[str]) -> dict[str, int]:
    """Best-effort Vulture run over the repo's Python files -> ``{file: finding_count}``.

    Returns ``{}`` on any absence/failure (missing binary, no Python files, a crashing
    subprocess) -- a detector's non-availability degrades the census to judgment for that
    instrument rather than raising.
    """
    if shutil.which("vulture") is None:
        return {}
    py_files = [f for f in files if f.endswith(".py")]
    if not py_files:
        return {}
    try:
        proc = subprocess.run(
            ["vulture", *py_files], cwd=repo_root, capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    counts: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        # vulture output: "path/to/file.py:12: unused variable 'x' (60% confidence)"
        path = line.split(":", 1)[0].strip()
        if path:
            counts[path] = counts.get(path, 0) + 1
    return counts


# The default runner set: only instruments R1 confirmed pinned/probeable are wired here.
# A detector absent from this dict (or whose runner returns {}) simply contributes zero
# findings -- the census degrades per-instrument, never all-or-nothing.
DEFAULT_DETECTOR_RUNNERS: dict[str, DetectorRunner] = {
    "vulture": _vulture_findings,
}


def _line_count(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return sum(1 for _ in fh) or 1
    except OSError:
        return 1


def run_census(
    repo_root: str,
    *,
    density_threshold: float = DEFAULT_DENSITY_THRESHOLD,
    detector_runners: Optional[dict[str, DetectorRunner]] = None,
) -> CensusReport:
    """Run the B7 deterministic detector census across ``repo_root``: enumerate every source
    file, run each configured detector, score per-file slop density (findings per line), and
    bound the LLM-judged set to files that carry >=1 detector finding OR a density score
    above ``density_threshold`` -- never the whole repo. A missing/failing detector runner
    contributes zero findings rather than raising (D7 graceful degradation).
    """
    runners = DEFAULT_DETECTOR_RUNNERS if detector_runners is None else detector_runners
    files = discover_source_files(repo_root)
    total = len(files)

    finding_counts: dict[str, int] = {f: 0 for f in files}
    for runner in runners.values():
        for path, n in runner(repo_root, files).items():
            if path in finding_counts:
                finding_counts[path] += n

    per_file: list[FileCensus] = []
    judged: list[str] = []
    for f in files:
        findings = finding_counts.get(f, 0)
        line_count = _line_count(os.path.join(repo_root, f))
        density = findings / line_count
        per_file.append(FileCensus(path=f, finding_count=findings, density_score=density))
        if findings >= 1 or density > density_threshold:
            judged.append(f)

    return CensusReport(
        total_source_file_count=total,
        judged_file_count=len(judged),
        judged_files=judged,
        matrix=instrument_matrix(),
        density_threshold=density_threshold,
        per_file=per_file,
    )
