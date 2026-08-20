"""Bounded ``/af-clean`` driver for the audited P-1 ledger-reader consolidation.

This module describes the cleanup operation; it does not implement ledger policy or construct the
consolidation patch.  A candidate patch must be built separately, persisted as a unified diff, and
then admitted through :func:`apply_p1_diff`.  Keeping that order means the executable-code change
cannot reach the real checkout before its replay witnesses and blind review have succeeded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .executable_diff import (
    ExecutableDiffResult,
    WitnessCommand,
    apply_bounded_executable_diff,
)
from .findings import CLASS_CONSOLIDATION, Finding, Location


P1_RULE = "p1-ledger-reader-consolidation"
P1_LOCATIONS = (
    ("knowledge/ml_registry/bootstrap.py", 131),
    ("knowledge/ml_registry/write_path.py", 149),
    ("knowledge/ml_registry/floor.py", 456),
    ("knowledge/ml_registry/cli.py", 116),
)
P1_DIFF_ALLOWLIST = frozenset({
    "knowledge/ml_registry/bootstrap.py",
    "knowledge/ml_registry/write_path.py",
    "knowledge/ml_registry/floor.py",
    "knowledge/ml_registry/cli.py",
    "knowledge/ml_registry/contracts/ledger_v2.py",
    "knowledge/ml_registry/tests/test_ledger_v2_golden.py",
})
P1_WITNESSES = (
    WitnessCommand((
        "env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
        "knowledge/ml_registry/tests/test_ledger_v2_golden.py", "-q", "-p", "no:cacheprovider",
    )),
    WitnessCommand((
        "env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
        "knowledge/ml_registry/tests", "-q", "-p", "no:cacheprovider",
    )),
)


def p1_findings() -> tuple[Finding, ...]:
    """Return the four located, mechanically admissible P-1 findings."""
    return tuple(
        Finding(
            rule=P1_RULE,
            tier="judgment",
            location=Location(file=file, line=line),
            pole="fragmentation",
            change_class=CLASS_CONSOLIDATION,
            chunks=("header handling", "row parsing", "error handling", "caller projection"),
            is_dry=True,
            observable="co-change",
            proposal="consolidate into the versioned ledger contract without caller flags",
        )
        for file, line in P1_LOCATIONS
    )


def read_prebuilt_diff(path: str | Path) -> str:
    """Read a separately prepared UTF-8 unified diff without interpreting its policy."""
    diff_path = Path(path)
    if not diff_path.is_file():
        raise ValueError(f"prebuilt P-1 diff is not a file: {diff_path}")
    try:
        content = diff_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"prebuilt P-1 diff is not UTF-8: {diff_path}") from exc
    if not content.strip():
        raise ValueError(f"prebuilt P-1 diff is empty: {diff_path}")
    return content


def generate_prebuilt_diff(
    repo_root: str | Path,
    candidate_ref: str,
    *,
    base_ref: str = "HEAD",
) -> str:
    """Generate the bounded patch from two already-materialised git revisions.

    Revisions, rather than a dirty worktree, make the proposed bytes reproducible.  The returned
    text is intentionally not applied or written; callers persist it outside the target checkout
    and later pass that file to :func:`apply_p1_diff`.
    """
    root = Path(repo_root).resolve()
    argv = [
        "git", "diff", "--no-ext-diff", "--binary", base_ref, candidate_ref, "--",
        *sorted(P1_DIFF_ALLOWLIST),
    ]
    result = subprocess.run(argv, cwd=root, capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError(f"could not generate prebuilt P-1 diff: {result.stderr.strip() or 'no output'}")
    if not result.stdout.strip():
        raise ValueError("generated P-1 diff is empty")
    return result.stdout


def apply_p1_diff(
    repo_root: str | Path,
    diff_path: str | Path,
    **adapter_overrides: object,
) -> ExecutableDiffResult:
    """Admit, witness, blindly verify, and apply the exact prebuilt P-1 patch.

    ``adapter_overrides`` exists only to replace process runners in tests.  None of the bounded
    rule, locations, paths, witnesses, or change class can be overridden.
    """
    forbidden = {
        "diff", "findings", "expected_rule", "expected_locations", "diff_allowlist",
        "witnesses", "change_class",
    }.intersection(adapter_overrides)
    if forbidden:
        raise TypeError(f"P-1 safety boundary cannot be overridden: {sorted(forbidden)!r}")
    return apply_bounded_executable_diff(
        repo_root=repo_root,
        diff=read_prebuilt_diff(diff_path),
        findings=p1_findings(),
        expected_rule=P1_RULE,
        expected_locations=frozenset(P1_LOCATIONS),
        diff_allowlist=P1_DIFF_ALLOWLIST,
        witnesses=P1_WITNESSES,
        change_class=CLASS_CONSOLIDATION,
        **adapter_overrides,
    )
