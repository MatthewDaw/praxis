"""Located reachability proof for private CLI residue, updated one deletion at a time."""

from __future__ import annotations

import ast
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "cli/registry.py"

EXPECTED_DEAD_SYMBOLS = {
    "_parse_intervention",
    "_refuse_a_campaign_with_no_floor",
    "_update_registered_model",
}
EXPECTED_ABSENT_SYMBOLS: set[str] = {"_checked_model_budgets"}


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
