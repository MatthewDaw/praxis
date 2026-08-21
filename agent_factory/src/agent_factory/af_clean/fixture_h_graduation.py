"""Bounded af-clean driver for graduating sports_analysis Fixture H."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .executable_diff import ExecutableDiffResult, WitnessCommand, apply_bounded_executable_diff
from .findings import CLASS_TEST_GRADUATION, Finding, Location


RULE = "sports-fixture-h-test-graduation"
PATH = "tests/integration/test_remaining_foundation_characterization.py"
LOCATIONS = ((PATH, 19),)
DIFF_ALLOWLIST = frozenset({PATH})
PRAXIS_ROOT = "/private/tmp/praxis-af-clean-test-graduation"
WITNESSES = (
    WitnessCommand((
        "env", f"PRAXIS_ROOT={PRAXIS_ROOT}", f"PRAXIS_REPO_ROOT={PRAXIS_ROOT}",
        "uv", "run", "pytest", PATH, "-q", "-k", "fixture_h",
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
            location=Location(file=PATH, line=19),
            change_class=CLASS_TEST_GRADUATION,
            chunks=("strict xfail marker", "Fixture H manifest-coverage audit"),
            proposal="remove only Fixture H's strict xfail marker after focused and collection witnesses",
        ),
    )


def graduation_diff(repo_root: str | Path, graduation_ref: str) -> str:
    result = subprocess.run(
        ["git", "show", "--format=", "--no-ext-diff", "--binary", graduation_ref, "--", PATH],
        cwd=Path(repo_root).resolve(),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError(result.stderr.strip() or "graduation ref produced no Fixture H diff")
    return result.stdout


def apply(repo_root: str | Path, graduation_ref: str, **overrides: object) -> ExecutableDiffResult:
    forbidden = {
        "diff", "findings", "expected_rule", "expected_locations", "diff_allowlist",
        "witnesses", "change_class",
    }.intersection(overrides)
    if forbidden:
        raise TypeError(f"Fixture H safety boundary cannot be overridden: {sorted(forbidden)!r}")
    return apply_bounded_executable_diff(
        repo_root=repo_root,
        diff=graduation_diff(repo_root, graduation_ref),
        findings=findings(),
        expected_rule=RULE,
        expected_locations=frozenset(LOCATIONS),
        diff_allowlist=DIFF_ALLOWLIST,
        witnesses=WITNESSES,
        change_class=CLASS_TEST_GRADUATION,
        **overrides,
    )


__all__ = ["apply", "findings", "graduation_diff"]
