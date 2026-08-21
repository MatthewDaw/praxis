"""Bounded `/af-clean` driver for obsolete private ML-registry CLI code."""

from __future__ import annotations

from pathlib import Path

from .executable_diff import ExecutableDiffResult, WitnessCommand, apply_bounded_executable_diff
from .findings import CLASS_CODE_DELETION, Finding, Location
from .p8_cli_split import read_prebuilt_diff


RULE = "ml-registry-private-cli-dispatch"
LOCATIONS = frozenset({("knowledge/ml_registry/cli/registry.py", 711)})
ALLOWLIST = frozenset({
    "knowledge/ml_registry/cli/registry.py",
})
WITNESSES = (
    WitnessCommand((
        "env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
        "knowledge/ml_registry/tests/test_registry_cli_golden.py",
        "knowledge/ml_registry/tests/test_cli.py", "-q", "-p", "no:cacheprovider",
    )),
    WitnessCommand((
        "env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
        "knowledge/ml_registry/tests", "-q", "-p", "no:cacheprovider",
    )),
)


def finding() -> Finding:
    return Finding(
        rule=RULE,
        tier="enforce",
        location=Location("knowledge/ml_registry/cli/registry.py", 711),
        pole="bloat",
        change_class=CLASS_CODE_DELETION,
        proposal=(
            "delete only the unreachable private pre-cutover dispatcher; preserve every helper, "
            "IDEA bridge, public command, and canonical historical import/export for a later, "
            "independently witnessed batch"
        ),
    )


def apply_cli_dead_code_diff(
    repo_root: str | Path, diff_path: str | Path, **overrides: object,
) -> ExecutableDiffResult:
    forbidden = {
        "diff", "findings", "expected_rule", "expected_locations", "diff_allowlist",
        "witnesses", "change_class",
    }.intersection(overrides)
    if forbidden:
        raise TypeError(f"CLI dead-code safety boundary cannot be overridden: {sorted(forbidden)!r}")
    return apply_bounded_executable_diff(
        repo_root=repo_root,
        diff=read_prebuilt_diff(diff_path),
        findings=(finding(),),
        expected_rule=RULE,
        expected_locations=LOCATIONS,
        diff_allowlist=ALLOWLIST,
        witnesses=WITNESSES,
        change_class=CLASS_CODE_DELETION,
        **overrides,
    )
