"""Located reachability proof for private CLI residue, updated one deletion at a time."""

from __future__ import annotations

import ast
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "cli/registry.py"

EXPECTED_DEAD_SYMBOLS = {
}
EXPECTED_ABSENT_SYMBOLS: set[str] = {
    "_checked_model_budgets", "_parse_intervention", "_refuse_a_campaign_with_no_floor",
    "_update_registered_model",
}
EXPECTED_DEAD_IMPORTS = {
    "MAX_DISCOVERED_IDEAS_FIELD", "METRIC_FIELD",
    "MODEL_DEFAULTS", "UNLIMITED_DISCOVERED_IDEAS",
    "load_ledger_commits", "mutate_model",
    "register_model", "register_trial",
    "supersede_trial",
}
EXPECTED_ABSENT_IMPORTS: set[str] = {
    "DEFAULT_SIGMAS", "TicketIndex", "adjudicate_trial", "load_ledger_values",
    "flagged_trials", "guard_baseline_move", "guard_model_mutation", "model_to_projects",
    "Intervention", "MODEL", "acknowledge_diagnosis", "campaign_completeness", "campaign_status",
    "adjudicate_verdict", "format_status", "per_axis_yield", "project_to_models", "reset_ratchet",
    "record_keep_pushing_marker", "record_out_of_diff_change", "supervise_campaign", "validate_fact",
    "register_model_with_baseline", "retire_harness",
}


def _inventory() -> tuple[set[str], dict[str, int]]:
    tree = ast.parse(MODULE.read_text())
    definitions = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    loads: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            loads[node.id] = loads.get(node.id, 0) + 1
    return definitions, loads


def _imports() -> set[str]:
    tree = ast.parse(MODULE.read_text())
    return {
        alias.asname or alias.name
        for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }


def test_each_located_private_cli_symbol_is_defined_but_has_no_reachable_reference():
    definitions, loads = _inventory()
    for symbol in EXPECTED_DEAD_SYMBOLS:
        assert symbol in definitions
        assert loads.get(symbol, 0) == 0


def test_each_witnessed_private_cli_deletion_is_absent():
    definitions, loads = _inventory()
    for symbol in EXPECTED_ABSENT_SYMBOLS:
        assert symbol not in definitions
        assert loads.get(symbol, 0) == 0


def test_each_located_private_cli_import_is_present_but_unreferenced():
    imports = _imports()
    _, loads = _inventory()
    for name in EXPECTED_DEAD_IMPORTS:
        assert name in imports
        assert loads.get(name, 0) == 0


def test_each_witnessed_private_cli_import_deletion_is_absent():
    imports = _imports()
    _, loads = _inventory()
    for name in EXPECTED_ABSENT_IMPORTS:
        assert name not in imports
        assert loads.get(name, 0) == 0
