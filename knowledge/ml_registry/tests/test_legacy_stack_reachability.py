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
ARTIFACT_STORE_BEHAVIOR_MAP = {
    "test_ingest_copies_content_addressed_blob_and_replay_is_idempotent":
        "test_artifact_identity_and_size_are_derived_only_from_stored_bytes",
    "test_ingest_refuses_source_checksum_or_size_mismatch":
        "test_artifact_creation_refuses_caller_claimed_identity_checksum_or_size",
    "test_artifact_id_drift_is_refused_without_appending_history":
        "test_artifact_creation_refuses_caller_claimed_identity_checksum_or_size",
    "test_replay_detects_event_payload_and_hash_chain_tampering":
        "test_event_tamper_and_blob_tamper_are_detected",
    "test_replay_detects_a_broken_link_between_valid_event_documents":
        "test_event_tamper_and_blob_tamper_are_detected",
    "test_verify_detects_blob_tampering": "test_event_tamper_and_blob_tamper_are_detected",
    "test_projection_failure_leaves_event_replayable_and_rebuild_repairs_view":
        "test_event_before_projection_recovers_after_crash",
    "test_concurrent_distinct_ingests_form_one_contiguous_hash_chain":
        "test_single_writer_serializes_concurrent_events",
    "test_finalization_is_one_idempotent_event_and_campaign_promotion_is_unique":
        "test_finalization_is_one_registry_event_and_returns_canonical_views",
    "test_nonfinite_event_time_is_refused_before_history_is_written":
        "test_event_log_refuses_nonfinite_or_boolean_time_before_writing",
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


def test_each_legacy_artifact_store_behavior_has_a_concrete_canonical_test() -> None:
    canonical = "\n".join(
        (ROOT / path).read_text()
        for path in (
            "knowledge/ml_registry/tests/test_standard_registry.py",
            "knowledge/ml_registry/tests/test_registry_finalizer.py",
        )
    )
    legacy = ROOT / "knowledge/ml_registry/tests/test_artifact_store.py"
    if str(legacy.relative_to(ROOT)) not in EXPECTED_ABSENT_CALLERS:
        legacy_text = legacy.read_text()
        assert all(name in legacy_text for name in ARTIFACT_STORE_BEHAVIOR_MAP)
    assert all(name in canonical for name in ARTIFACT_STORE_BEHAVIOR_MAP.values())


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
