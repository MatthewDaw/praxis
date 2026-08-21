"""Bounded `/af-clean` driver for helpers orphaned by the private CLI deletion."""

from __future__ import annotations

from pathlib import Path

from .executable_diff import ExecutableDiffResult, WitnessCommand, apply_bounded_executable_diff
from .findings import CLASS_CODE_DELETION, Finding, Location
from .p8_cli_split import read_prebuilt_diff


RULE = "ml-registry-private-cli-dead-helpers"
LOCATIONS = frozenset({
    ("knowledge/ml_registry/cli/registry.py", 176),
    ("knowledge/ml_registry/cli/registry.py", 312),
})
ALLOWLIST = frozenset({"knowledge/ml_registry/cli/registry.py"})
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
    WitnessCommand((
        "uv", "run", "ruff", "check", "knowledge/ml_registry/cli/registry.py",
    )),
)


def findings() -> tuple[Finding, ...]:
    proposal = (
        "delete only executable helpers and imports whose sole caller was the already-deleted "
        "private pre-cutover dispatcher; preserve every IDEA bridge and public helper"
    )
    return tuple(
        Finding(
            rule=RULE, tier="enforce", location=Location(path, line), pole="bloat",
            change_class=CLASS_CODE_DELETION, proposal=proposal,
        )
        for path, line in sorted(LOCATIONS)
    )


def apply_cli_dead_helpers_diff(
    repo_root: str | Path, diff_path: str | Path, **overrides: object,
) -> ExecutableDiffResult:
    forbidden = {
        "diff", "findings", "expected_rule", "expected_locations", "diff_allowlist",
        "witnesses", "change_class",
    }.intersection(overrides)
    if forbidden:
        raise TypeError(f"CLI helper safety boundary cannot be overridden: {sorted(forbidden)!r}")
    return apply_bounded_executable_diff(
        repo_root=repo_root,
        diff=read_prebuilt_diff(diff_path),
        findings=findings(),
        expected_rule=RULE,
        expected_locations=LOCATIONS,
        diff_allowlist=ALLOWLIST,
        witnesses=WITNESSES,
        change_class=CLASS_CODE_DELETION,
        **overrides,
    )
