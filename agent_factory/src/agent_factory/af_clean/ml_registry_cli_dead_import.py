"""One-import-statement-at-a-time `/af-clean` boundary for private CLI residue."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .executable_diff import ExecutableDiffResult, WitnessCommand, apply_bounded_executable_diff
from .findings import CLASS_CODE_DELETION, Finding, Location
from .p8_cli_split import read_prebuilt_diff

SOURCE = "knowledge/ml_registry/cli/registry.py"
TEST = "knowledge/ml_registry/tests/test_cli_dead_reachability.py"


@dataclass(frozen=True)
class ImportBatch:
    key: str
    line: int
    names: tuple[str, ...]

    @property
    def rule(self) -> str:
        return f"ml-registry-private-cli-import-{self.key}"


BATCHES = {batch.key: batch for batch in (
    ImportBatch("cross-project", 31, ("TicketIndex", "model_to_projects", "project_to_models")),
    ImportBatch("floor", 31, ("DEFAULT_SIGMAS", "adjudicate_trial", "load_ledger_values", "register_model_with_baseline", "retire_harness")),
    ImportBatch("guards", 31, ("guard_baseline_move", "guard_model_mutation")),
    ImportBatch("lifecycle", 38, ("flagged_trials", "per_axis_yield")),
    ImportBatch("schema", 50, ("MODEL", "validate_fact")),
    ImportBatch("supervisor", 51, ("Intervention", "record_keep_pushing_marker", "record_out_of_diff_change", "supervise_campaign")),
    ImportBatch("completeness", 51, ("campaign_completeness",)),
    ImportBatch("report", 52, ("acknowledge_diagnosis", "campaign_status", "format_status")),
    ImportBatch("verdict", 54, ("adjudicate_verdict", "reset_ratchet")),
    ImportBatch("write-path", 55, ("MAX_DISCOVERED_IDEAS_FIELD", "METRIC_FIELD", "MODEL_DEFAULTS", "UNLIMITED_DISCOVERED_IDEAS", "load_ledger_commits", "mutate_model", "register_model", "register_trial", "supersede_trial")),
)}
ALLOWLIST = frozenset({SOURCE, TEST})
WITNESSES = (
    WitnessCommand(("env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest", TEST,
                    "knowledge/ml_registry/tests/test_registry_cli_golden.py",
                    "knowledge/ml_registry/tests/test_cli.py", "-q", "-p", "no:cacheprovider")),
    WitnessCommand(("env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
                    "knowledge/ml_registry/tests", "-q", "-p", "no:cacheprovider")),
)


def finding(batch: ImportBatch) -> Finding:
    return Finding(
        rule=batch.rule, tier="enforce", location=Location(SOURCE, batch.line), pole="bloat",
        change_class=CLASS_CODE_DELETION,
        proposal=(f"delete only unreferenced private CLI imports {batch.names!r}; move them "
                  "from the AST-proven dead-import set to witnessed-absent imports"),
    )


def apply_dead_import_diff(repo_root: str | Path, diff_path: str | Path, *, key: str, **overrides: object) -> ExecutableDiffResult:
    try:
        batch = BATCHES[key]
    except KeyError as exc:
        raise ValueError(f"unknown bounded CLI import batch {key!r}") from exc
    forbidden = {"diff", "findings", "expected_rule", "expected_locations", "diff_allowlist", "witnesses", "change_class"}.intersection(overrides)
    if forbidden:
        raise TypeError(f"CLI import safety boundary cannot be overridden: {sorted(forbidden)!r}")
    return apply_bounded_executable_diff(
        repo_root=repo_root, diff=read_prebuilt_diff(diff_path), findings=(finding(batch),),
        expected_rule=batch.rule, expected_locations=frozenset({(SOURCE, batch.line)}),
        diff_allowlist=ALLOWLIST, witnesses=WITNESSES, change_class=CLASS_CODE_DELETION,
        **overrides,
    )
