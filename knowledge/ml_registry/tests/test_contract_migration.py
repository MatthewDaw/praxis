from __future__ import annotations

from copy import deepcopy

import pytest

from knowledge.ml_registry.contracts import LedgerRowV2, LedgerV2, migrate_ledger, migrate_mapping
from knowledge.ml_registry.contracts.migration import migrate_registry_ratchet_lineage
from knowledge.ml_registry.contracts._validation import ContractError


OUTCOME_V0 = {
    "campaign_id": "fixture", "outcome": "COMPLETE", "reason": "verified",
    "attempt": 1, "production_alias": {"model_id": "model-fixture", "version": 1, "alias": "production"},
}


def test_unversioned_fixture_migration_is_additive_pure_and_idempotent():
    original = deepcopy(OUTCOME_V0)
    migrated = migrate_mapping("campaign_outcome", OUTCOME_V0)
    assert OUTCOME_V0 == original
    assert migrated == {"schema_version": 2, **original}
    assert migrate_mapping("campaign_outcome", migrated) == migrated


def test_mapping_migration_never_aliases_or_infers_missing_fields():
    with pytest.raises(ContractError, match="unknown campaign outcome fields"):
        migrate_mapping("campaign_outcome", {**OUTCOME_V0, "result": "complete"})
    incomplete = dict(OUTCOME_V0)
    incomplete.pop("production_alias")
    with pytest.raises(ContractError, match="requires a canonical production alias"):
        migrate_mapping("campaign_outcome", incomplete)


def test_mapping_migration_rejects_mismatched_and_future_versions():
    with pytest.raises(ContractError, match="does not match embedded"):
        migrate_mapping("campaign_outcome", {"schema_version": 2, **OUTCOME_V0}, source_version=0)
    with pytest.raises(ContractError, match="future"):
        migrate_mapping("campaign_outcome", {"schema_version": 99, **OUTCOME_V0})


def test_ledger_v2_offline_migration_is_byte_stable():
    current = LedgerV2.from_rows([LedgerRowV2("sha:arm", .8, 1.0, "ok", "arm", 2.0, 3)]).serialize()
    assert migrate_ledger(current) == current


@pytest.mark.parametrize("legacy", [
    "commit\tmetric_value\tmemory_gb\tstatus\tdescription\nsha\t.8\t1\tok\tarm\n",
    "commit\tmetric_value\tmemory_gb\tstatus\tdescription\tthroughput\nsha\t.8\t1\tok\tarm\t2\n",
])
def test_legacy_ledgers_are_refused_instead_of_inventing_adjudication_inputs(legacy):
    with pytest.raises(ContractError, match="must be emitted by the writer"):
        migrate_ledger(legacy)


def test_ratchet_lineage_migration_marks_historical_evidence_unknown_without_inventing_it():
    raw = {"facts": [
        {"id": "model-1", "category": "model", "meta": {"baseline": "base"}, "derivedFrom": []},
        {"id": "trial-1", "category": "trial", "meta": {
            "model_id": "model-1", "commit": "candidate",
        }, "derivedFrom": ["idea-1"]},
    ]}
    migrated = migrate_registry_ratchet_lineage(raw)
    assert "active_adoption_lineage" not in raw["facts"][0]["meta"]
    model = migrated["facts"][0]["meta"]
    trial = migrated["facts"][1]["meta"]
    assert model["active_adoption_lineage"] == "root:model-1:base"
    assert model["ratchet_lineage_migration"] == "counterfactual_unknown"
    assert trial["base_lineage_id"].startswith("legacy-unknown:")
    assert trial["base_commit"] == "legacy-unknown"
    assert trial["ratchet_evidence"] == "counterfactual_unknown"


def test_ratchet_lineage_migration_recovers_the_active_adoptions_direct_parent():
    raw = {"facts": [
        {"id": "model-1", "category": "model", "meta": {
            "baseline": "winner", "previous_baseline": "base",
        }},
        {"id": "idea-1", "category": "idea", "meta": {
            "model_id": "model-1", "status": "adopted", "adopted_trial_id": "trial-win",
        }},
        {"id": "trial-win", "category": "trial", "meta": {
            "model_id": "model-1", "commit": "winner",
        }},
    ]}
    model = migrate_registry_ratchet_lineage(raw)["facts"][0]["meta"]
    assert model["active_adoption_lineage"] == "adoption:trial-win"
    lineage = model["adoption_lineages"]["adoption:trial-win"]
    assert lineage["parent_lineage_id"] == "root:model-1:base"
    assert lineage["parent_baseline_commit"] == "base"
