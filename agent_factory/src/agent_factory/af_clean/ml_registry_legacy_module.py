"""One-module-at-a-time `/af-clean` boundary for unreachable retired registry code."""

from __future__ import annotations

from pathlib import Path

from .executable_diff import ExecutableDiffResult, WitnessCommand, apply_bounded_executable_diff
from .findings import CLASS_CODE_DELETION, Finding, Location
from .p8_cli_split import read_prebuilt_diff


REACHABILITY_TEST = "knowledge/ml_registry/tests/test_legacy_stack_reachability.py"
MODULES = {
    "finalize": "knowledge/ml_registry/services/finalize.py",
}


def apply_legacy_module_diff(
    repo_root: str | Path, diff_path: str | Path, *, name: str, **overrides: object,
) -> ExecutableDiffResult:
    try:
        source = MODULES[name]
    except KeyError as exc:
        raise ValueError(f"unknown legacy module {name!r}") from exc
    rule = f"ml-registry-legacy-module-{name.replace('_', '-')}"
    finding = Finding(
        rule=rule, tier="enforce", location=Location(source, 1), pole="bloat",
        change_class=CLASS_CODE_DELETION,
        proposal=("delete only this AST-proven unreachable legacy module after its public authority "
                  "and test callers have moved to canonical registry services"),
    )
    forbidden = {
        "diff", "findings", "expected_rule", "expected_locations", "diff_allowlist",
        "witnesses", "change_class",
    }.intersection(overrides)
    if forbidden:
        raise TypeError(f"legacy-module safety boundary cannot be overridden: {sorted(forbidden)!r}")
    return apply_bounded_executable_diff(
        repo_root=repo_root, diff=read_prebuilt_diff(diff_path), findings=(finding,),
        expected_rule=rule, expected_locations=frozenset({(source, 1)}),
        diff_allowlist=frozenset({source, REACHABILITY_TEST}),
        witnesses=(
            WitnessCommand(("uv", "run", "pytest", "-q", REACHABILITY_TEST,
                            "knowledge/ml_registry/tests/test_registry_finalizer.py",
                            "knowledge/ml_registry/tests/test_registry_completeness.py")),
            WitnessCommand(("uv", "run", "pytest", "-q", "knowledge/ml_registry/tests")),
        ),
        change_class=CLASS_CODE_DELETION, **overrides,
    )
