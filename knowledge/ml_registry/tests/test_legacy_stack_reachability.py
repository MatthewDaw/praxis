"""Mechanical cutover proof for the retired pre-registry storage stack."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
LEGACY_CALLERS = {
    "knowledge/ml_registry/tests/test_finalize.py": {
        "knowledge.ml_registry.services.finalize",
        "knowledge.ml_registry.completeness",
    },
    "knowledge/ml_registry/tests/test_artifact_store.py": {
        "knowledge.ml_registry.storage.artifact_store",
    },
    "knowledge/ml_registry/tests/test_completeness.py": {
        "knowledge.ml_registry.completeness",
    },
}
CANONICAL_OBLIGATIONS = {
    "knowledge/ml_registry/tests/test_finalize.py": (
        "knowledge/ml_registry/tests/test_registry_finalizer.py",
        "knowledge/ml_registry/tests/test_registry_completeness.py",
    ),
    "knowledge/ml_registry/tests/test_artifact_store.py": (
        "knowledge/ml_registry/tests/test_standard_registry.py",
        "knowledge/ml_registry/tests/test_artifact_projection_golden.py",
    ),
    "knowledge/ml_registry/tests/test_completeness.py": (
        "knowledge/ml_registry/tests/test_registry_completeness.py",
    ),
}
EXPECTED_ABSENT_CALLERS = {"knowledge/ml_registry/tests/test_finalize.py"}
LEGACY_MODULES = {
    "knowledge.ml_registry.services.finalize": "knowledge/ml_registry/services/finalize.py",
}
MODULE_REPLACEMENT_OBLIGATIONS = {
    "knowledge.ml_registry.services.finalize": {
        "knowledge/ml_registry/services/registry_finalize.py": (
            "class RegistryFinalizeService",
            "def move_production",
        ),
        "knowledge/ml_registry/tests/test_registry_finalizer.py": (
            "test_finalization_is_one_registry_event_and_returns_canonical_views",
            "test_completeness_compatibility_and_blob_are_hard_gates",
        ),
    },
}
EXPECTED_DEAD_MODULES = {"knowledge.ml_registry.services.finalize"}
EXPECTED_ABSENT_MODULES: set[str] = set()


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }


def test_each_legacy_test_caller_has_explicit_canonical_replacement_obligations() -> None:
    for caller, legacy_imports in LEGACY_CALLERS.items():
        path = ROOT / caller
        if caller in EXPECTED_ABSENT_CALLERS:
            assert not path.exists()
            continue
        assert legacy_imports <= _imports(path)
        for replacement in CANONICAL_OBLIGATIONS[caller]:
            replacement_path = ROOT / replacement
            assert replacement_path.is_file() and replacement_path.stat().st_size > 0


def test_legacy_modules_are_unreachable_or_witnessed_absent() -> None:
    imports = {
        module
        for path in (ROOT / "knowledge/ml_registry").rglob("*.py")
        for module in _imports(path)
    }
    for module, source in LEGACY_MODULES.items():
        for replacement, required_symbols in MODULE_REPLACEMENT_OBLIGATIONS[module].items():
            replacement_text = (ROOT / replacement).read_text()
            assert all(symbol in replacement_text for symbol in required_symbols)
        path = ROOT / source
        if module in EXPECTED_ABSENT_MODULES:
            assert not path.exists()
        else:
            assert module in EXPECTED_DEAD_MODULES
            assert path.is_file()
            assert module not in imports
