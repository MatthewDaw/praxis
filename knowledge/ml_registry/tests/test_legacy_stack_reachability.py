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
COMPLETENESS_BEHAVIOR_MAP = {
    "test_an_empty_stage_blocks_rather_than_closing_silently":
        "test_empty_open_and_thin_stages_have_distinct_blockers",
    "test_an_open_stage_blocks": "test_empty_open_and_thin_stages_have_distinct_blockers",
    "test_a_thin_stage_blocks_even_though_it_closed":
        "test_empty_open_and_thin_stages_have_distinct_blockers",
    "test_a_voided_arm_blocks_because_it_is_unmeasured":
        "test_latest_run_status_and_verdict_drive_coverage",
    "test_in_flight_and_awaiting_adjudication_trials_never_measure_or_answer":
        "test_latest_run_status_and_verdict_drive_coverage",
    "test_unfair_abandoned_latest_trials_are_retryable_not_measurements":
        "test_latest_run_status_and_verdict_drive_coverage",
    "test_no_op_incumbent_remeasurement_does_not_satisfy_measured_floor":
        "test_both_noop_encodings_are_answered_but_not_measured",
    "test_latest_fair_retry_wins_over_an_older_void":
        "test_latest_run_status_and_verdict_drive_coverage",
    "test_latest_void_retry_state_wins_over_an_older_fair_result":
        "test_latest_retry_and_noop_do_not_populate_or_close_stage",
    "test_unreachable_arms_do_not_block_completion":
        "test_rejected_dependency_makes_dependent_unreachable_but_retry_does_not",
    "test_an_arm_whose_dependency_is_not_an_idea_does_not_hold_its_stage_open":
        "test_view_rejects_unknown_stage_or_dependency",
    "test_completeness_uses_meta_stage_not_just_axis":
        "test_view_rejects_unknown_stage_or_dependency",
    "test_an_unregistered_model_is_refused":
        "test_view_joins_only_on_fact_id_and_canonicalizes_dependencies",
    "test_a_campaign_with_no_convergence_run_is_not_finished":
        "test_completion_requires_current_compatible_verified_production_lineage",
    "test_all_stages_closed_but_truthy_legacy_convergence_is_not_done":
        "test_completion_requires_current_compatible_verified_production_lineage",
    "test_malformed_truthy_convergence_does_not_complete_campaign":
        "test_missing_or_wrong_champion_version_is_wrong_lineage",
    "test_wrong_lineage_convergence_does_not_complete_campaign":
        "test_wrong_or_superseded_version_lineage_blocks",
    "test_stale_convergence_does_not_complete_campaign":
        "test_checksum_drift_is_stale_artifact",
    "test_convergence_can_be_waived_explicitly":
        "test_completion_requires_current_compatible_verified_production_lineage",
}
FINALIZER_BEHAVIOR_MAP = {
    "test_finalize_writes_one_promotion_and_canonical_completeness_accepts_it":
        "test_finalization_is_one_registry_event_and_returns_canonical_views",
    "test_every_precommit_failpoint_leaves_no_partial_finalization":
        "test_pending_projection_refuses_champion_race_before_event_append",
    "test_failure_after_commit_is_recovered_by_idempotent_retry":
        "test_crash_after_event_recovers_alias_and_finalization_together",
    "test_idempotent_retry_refuses_changed_finalization_payload":
        "test_full_payload_retry_is_idempotent_and_drift_is_refused",
    "test_finalize_rejects_wrong_current_lineage_without_writing":
        "test_only_current_champion_adopted_lineage_can_finalize",
    "test_finalize_rejects_a_promotion_after_its_adoption_lineage_changed":
        "test_only_current_champion_adopted_lineage_can_finalize",
    "test_finalize_rejects_tampered_artifact_upstream_and_compatibility":
        "test_completeness_compatibility_and_blob_are_hard_gates",
}
EXPECTED_ABSENT_CALLERS = {
    "knowledge/ml_registry/tests/test_artifact_store.py",
    "knowledge/ml_registry/tests/test_completeness.py",
    "knowledge/ml_registry/tests/test_finalize.py",
}
LEGACY_MODULES = {
    "knowledge.ml_registry.contracts.artifact_manifest":
        "knowledge/ml_registry/contracts/artifact_manifest.py",
    "knowledge.ml_registry.contracts.promotion": "knowledge/ml_registry/contracts/promotion.py",
    "knowledge.ml_registry.completeness": "knowledge/ml_registry/completeness.py",
    "knowledge.ml_registry.services.finalize": "knowledge/ml_registry/services/finalize.py",
    "knowledge.ml_registry.storage.artifact_store":
        "knowledge/ml_registry/storage/artifact_store.py",
}
MODULE_REPLACEMENT_OBLIGATIONS = {
    "knowledge.ml_registry.contracts.artifact_manifest": {
        "knowledge/ml_registry/domain/registry.py": ("class Artifact", "class ModelVersion"),
    },
    "knowledge.ml_registry.contracts.promotion": {
        "knowledge/ml_registry/domain/registry.py": ("class Alias", "class ModelVersion"),
    },
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
    "knowledge.ml_registry.completeness": {
        "knowledge/ml_registry/services/completeness.py": ("def campaign_completeness",),
        "knowledge/ml_registry/tests/test_registry_completeness.py": (
            "test_completion_requires_current_compatible_verified_production_lineage",
        ),
    },
    "knowledge.ml_registry.storage.artifact_store": {
        "knowledge/ml_registry/storage/registry.py": ("def create_artifact",),
        "knowledge/ml_registry/tests/test_standard_registry.py": (
            "test_event_tamper_and_blob_tamper_are_detected",
            "test_single_writer_serializes_concurrent_events",
        ),
    },
}
EXPECTED_DEAD_MODULES: set[str] = set()
EXPECTED_ABSENT_MODULES = {
    "knowledge.ml_registry.contracts.artifact_manifest",
    "knowledge.ml_registry.contracts.promotion",
    "knowledge.ml_registry.completeness",
    "knowledge.ml_registry.services.finalize",
    "knowledge.ml_registry.storage.artifact_store",
}


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


def test_each_relevant_legacy_completeness_behavior_has_a_canonical_test() -> None:
    canonical = "\n".join(
        (ROOT / path).read_text()
        for path in (
            "knowledge/ml_registry/tests/test_registry_completeness.py",
            "knowledge/ml_registry/tests/test_campaign_view.py",
        )
    )
    legacy = ROOT / "knowledge/ml_registry/tests/test_completeness.py"
    if str(legacy.relative_to(ROOT)) not in EXPECTED_ABSENT_CALLERS:
        legacy_text = legacy.read_text()
        assert all(name in legacy_text for name in COMPLETENESS_BEHAVIOR_MAP)
    assert all(name in canonical for name in COMPLETENESS_BEHAVIOR_MAP.values())


def test_each_retired_finalizer_behavior_has_a_concrete_canonical_test() -> None:
    canonical = (ROOT / "knowledge/ml_registry/tests/test_registry_finalizer.py").read_text()
    assert all(name in canonical for name in FINALIZER_BEHAVIOR_MAP.values())


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
