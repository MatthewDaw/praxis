"""One-symbol-at-a-time `/af-clean` boundary for unreachable private CLI helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .executable_diff import ExecutableDiffResult, WitnessCommand, apply_bounded_executable_diff
from .findings import CLASS_CODE_DELETION, Finding, Location
from .p8_cli_split import read_prebuilt_diff


SOURCE = "knowledge/ml_registry/cli/registry.py"
REACHABILITY_TEST = "knowledge/ml_registry/tests/test_cli_dead_reachability.py"


@dataclass(frozen=True)
class DeadSymbolBatch:
    symbol: str
    line: int

    @property
    def rule(self) -> str:
        return f"ml-registry-private-cli-symbol-{self.symbol.removeprefix('_')}"


BATCHES = {
    batch.symbol: batch for batch in (
        DeadSymbolBatch("_checked_model_budgets", 176),
        DeadSymbolBatch("_update_registered_model", 225),
        DeadSymbolBatch("_refuse_a_campaign_with_no_floor", 259),
        DeadSymbolBatch("_parse_intervention", 312),
    )
}
ALLOWLIST = frozenset({SOURCE, REACHABILITY_TEST})
WITNESSES = (
    WitnessCommand(("env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
                    REACHABILITY_TEST,
                    "knowledge/ml_registry/tests/test_registry_cli_golden.py",
                    "knowledge/ml_registry/tests/test_cli.py",
                    "-q", "-p", "no:cacheprovider")),
    WitnessCommand(("env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
                    "knowledge/ml_registry/tests", "-q", "-p", "no:cacheprovider")),
)


def finding(batch: DeadSymbolBatch) -> Finding:
    return Finding(
        rule=batch.rule, tier="enforce", location=Location(SOURCE, batch.line), pole="bloat",
        change_class=CLASS_CODE_DELETION,
        proposal=(f"delete only unreachable private CLI helper {batch.symbol}; move its name "
                  "from the AST-proven dead set to the witnessed-absent set"),
    )


def apply_dead_symbol_diff(
    repo_root: str | Path, diff_path: str | Path, *, symbol: str, **overrides: object,
) -> ExecutableDiffResult:
    try:
        batch = BATCHES[symbol]
    except KeyError as exc:
        raise ValueError(f"unknown bounded CLI symbol {symbol!r}") from exc
    forbidden = {"diff", "findings", "expected_rule", "expected_locations", "diff_allowlist", "witnesses", "change_class"}.intersection(overrides)
    if forbidden:
        raise TypeError(f"CLI symbol safety boundary cannot be overridden: {sorted(forbidden)!r}")
    return apply_bounded_executable_diff(
        repo_root=repo_root, diff=read_prebuilt_diff(diff_path), findings=(finding(batch),),
        expected_rule=batch.rule, expected_locations=frozenset({(SOURCE, batch.line)}),
        diff_allowlist=ALLOWLIST, witnesses=WITNESSES, change_class=CLASS_CODE_DELETION,
        **overrides,
    )
