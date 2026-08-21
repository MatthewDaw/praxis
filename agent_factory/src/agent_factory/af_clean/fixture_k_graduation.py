"""Bounded af-clean driver for graduating sports_analysis Fixture K."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .executable_diff import ExecutableDiffResult, WitnessCommand, apply_bounded_executable_diff
from .findings import CLASS_TEST_GRADUATION, Finding, Location


RULE = "sports-fixture-k-test-graduation"
PATH = "tests/integration/test_remaining_foundation_characterization.py"
LOCATIONS = ((PATH, 59),)
DIFF_ALLOWLIST = frozenset({PATH})
PRAXIS_ROOT = "/private/tmp/praxis-af-clean-test-graduation"
WITNESSES = (
    WitnessCommand((
        "env", f"PRAXIS_ROOT={PRAXIS_ROOT}", f"PRAXIS_REPO_ROOT={PRAXIS_ROOT}",
        "uv", "run", "pytest", PATH, "-q", "-k", "fixture_k",
    )),
    WitnessCommand((
        "env", f"PRAXIS_ROOT={PRAXIS_ROOT}", f"PRAXIS_REPO_ROOT={PRAXIS_ROOT}",
        "uv", "run", "pytest", "tests/experimentation/portfolio", "-q",
    )),
    WitnessCommand((
        "env", f"PRAXIS_ROOT={PRAXIS_ROOT}", f"PRAXIS_REPO_ROOT={PRAXIS_ROOT}",
        "make", "check-collect",
    )),
)


def findings() -> tuple[Finding, ...]:
    return (
        Finding(
            rule=RULE,
            tier="judgment",
            location=Location(file=PATH, line=59),
            change_class=CLASS_TEST_GRADUATION,
            chunks=("strict xfail marker", "five parametrized structural-integrity cases"),
            proposal="remove only Fixture K's strict xfail marker after focused and collection witnesses",
        ),
    )


def reverse_correction_diff(repo_root: str | Path, correction_ref: str) -> str:
    result = subprocess.run(
        [
            "git", "diff", "--no-ext-diff", "--binary",
            correction_ref, f"{correction_ref}^", "--", PATH,
        ],
        cwd=Path(repo_root).resolve(),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError(result.stderr.strip() or "correction ref produced no Fixture K reverse diff")
    return result.stdout


def apply(repo_root: str | Path, correction_ref: str, **overrides: object) -> ExecutableDiffResult:
    forbidden = {
        "diff", "findings", "expected_rule", "expected_locations", "diff_allowlist",
        "witnesses", "change_class",
    }.intersection(overrides)
    if forbidden:
        raise TypeError(f"Fixture K safety boundary cannot be overridden: {sorted(forbidden)!r}")
    return apply_bounded_executable_diff(
        repo_root=repo_root,
        diff=reverse_correction_diff(repo_root, correction_ref),
        findings=findings(),
        expected_rule=RULE,
        expected_locations=frozenset(LOCATIONS),
        diff_allowlist=DIFF_ALLOWLIST,
        witnesses=WITNESSES,
        change_class=CLASS_TEST_GRADUATION,
        **overrides,
    )


__all__ = ["apply", "findings", "reverse_correction_diff"]
