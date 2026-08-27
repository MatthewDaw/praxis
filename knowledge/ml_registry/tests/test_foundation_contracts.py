from __future__ import annotations

import pytest

from knowledge.ml_registry.contracts import (
    CampaignLease, CampaignOutcome, CampaignOutcomeRecord, CampaignSpec,
    LaunchIntent, LeaseSet, LedgerRowV2, LedgerV2,
)
from knowledge.ml_registry.contracts._validation import ContractError
from knowledge.ml_registry.domain.registry import Alias, Artifact, ModelVersion
from knowledge.ml_registry.domain.status import answers_question, fairly_measured, retryable, terminal


def test_ledger_v2_is_byte_stable_and_rejects_legacy_headers():
    ledger = LedgerV2.from_rows([LedgerRowV2("abc:arm", .8, 1.5, "ok", "arm", 12.0, 4)])
    assert LedgerV2.parse(ledger.serialize()) == ledger
    with pytest.raises(ContractError, match="not LedgerV2"):
        LedgerV2.parse("commit\tmetric_value\tmemory_gb\tstatus\tdescription\n")


def test_status_predicates_distinguish_terminal_fair_answer_and_retry():
    assert terminal("voided") and not fairly_measured("voided") and not answers_question("voided")
    assert retryable("voided")
    assert fairly_measured("succeeded") and answers_question("succeeded")
    assert retryable("failed") and not fairly_measured("failed")
    assert not terminal("complete") and not answers_question("complete")


def test_complete_outcome_requires_production_alias_proof():
    base = {"schema_version": 2, "campaign_id": "c", "outcome": "COMPLETE",
            "reason": "verified", "attempt": 1, "production_alias": None}
    with pytest.raises(ContractError, match="production alias"):
        CampaignOutcomeRecord.from_mapping(base)
    base["production_alias"] = {"model_id": "model-c", "alias": "production", "version": 1}
    assert CampaignOutcomeRecord.from_mapping(base).outcome is CampaignOutcome.COMPLETE


def test_code_ref_is_versioned_and_records_the_arm_commit():
    from knowledge.ml_registry.contracts.code_ref import CodeRef

    payload = {
        "schema_version": 1,
        "repo": "sports_analysis",
        "sha": "a" * 40,
        "base_sha": "b" * 40,
        "diff_hash": "c" * 64,
        "diff_lines": 7,
    }
    assert CodeRef.from_mapping(payload).to_mapping() == payload


def test_registry_artifact_version_alias_and_launch_intent_contracts():
    artifact = Artifact(
        artifact_id="fit", run_id="run-1", kind="weights", uri="blob://fit", bytes=4,
        schema_version="1",
    )
    version = ModelVersion(
        model_id="m", version=1, run_id=artifact.run_id, artifact_id=artifact.artifact_id,
        checksum="a" * 64, family_version="v1", code_sha="abc", preprocessing_hash="pre",
        calibration={}, thresholds={}, compat_result={"passed": True}, status="registered",
    )
    alias = Alias(
        model_id=version.model_id, alias="production", version=version.version,
        set_by="finalize", reason="compatibility passed", at=1.0,
    )
    assert version.artifact_id == artifact.artifact_id
    assert alias.alias == "production" and alias.version == version.version
    lease_payload = {"schema_version": 1, "lease_id": "lease-c", "campaign_id": "c", "owner": "worker",
                     "lane": "cpu", "device": "cpu:0", "exclusive": True, "cpu_threads": 2,
                     "cotenancy": "forbid", "throughput_gated": True, "state_root": "state/c",
                     "checkout": "worktrees/c", "cache_root": "cache/c", "ledger_path": "state/c/results.tsv",
                     "acquired_at": 1.0, "expires_at": 10.0}
    lease = CampaignLease.from_mapping(lease_payload)
    LeaseSet((lease,))
    intent = LaunchIntent.from_mapping({
        "schema_version": 1, "intent_id": "intent-c-1", "campaign_id": "c", "attempt": 1,
        "spec_digest": "b" * 64, "lease_ids": [lease.lease_id], "registry_trial_id": None,
        "state": "prepared", "created_at": 1.0, "pid": None, "pgid": None,
    })
    assert LaunchIntent.from_mapping(intent.to_mapping()) == intent


def test_campaign_spec_is_versioned_and_rejects_unknown_fields():
    payload = {
        "schema_version": 1, "campaign_id": "c", "model_id_policy": "mint", "axis": "01",
        "sport_scope": "shared", "target_ontology": "person", "metric": {"name": "f1"},
        "stages": [{"name": "representation"}], "corpora": [{"id": "fixture"}], "requires": [],
        "produces": [{"artifact_type": "weights"}], "supervision": {"mode": "composing"},
        "resources": {"lane": "cpu"}, "isolation": {"state_root": "state/c"},
        "production": {"protocol": "Detector"}, "inputs": [], "extends": [],
        "deterministic_incumbent": None,
        "learned_escalation": False,
    }
    assert CampaignSpec.from_mapping(payload).to_mapping() == {**payload, "sport_scope": ["shared"]}
    with pytest.raises(ContractError, match="unknown campaign spec"):
        CampaignSpec.from_mapping({**payload, "depends_on": ["upstream"]})
    split = {**payload, "split_policy": {"train_fraction": 0.9, "group_pure": True},
             "lookahead_window": {"kind": "whole_sequence"}}
    mapped = CampaignSpec.from_mapping(split).to_mapping()
    assert mapped["split_policy"] == {"train_fraction": 0.9, "group_pure": True}
    assert mapped["lookahead_window"] == {"kind": "whole_sequence"}
