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
