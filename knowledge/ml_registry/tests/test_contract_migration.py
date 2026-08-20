from __future__ import annotations

from copy import deepcopy

import pytest

from knowledge.ml_registry.contracts import LedgerRowV2, LedgerV2, migrate_ledger, migrate_mapping
from knowledge.ml_registry.contracts._validation import ContractError


OUTCOME_V0 = {
    "campaign_id": "fixture", "outcome": "COMPLETE", "reason": "verified",
    "attempt": 1, "promotion_record_id": "promotion-fixture",
}


def test_unversioned_fixture_migration_is_additive_pure_and_idempotent():
    original = deepcopy(OUTCOME_V0)
    migrated = migrate_mapping("campaign_outcome", OUTCOME_V0)
    assert OUTCOME_V0 == original
    assert migrated == {"schema_version": 1, **original}
    assert migrate_mapping("campaign_outcome", migrated) == migrated


def test_mapping_migration_never_aliases_or_infers_missing_fields():
    with pytest.raises(ContractError, match="unknown campaign outcome fields"):
        migrate_mapping("campaign_outcome", {**OUTCOME_V0, "result": "complete"})
    incomplete = dict(OUTCOME_V0)
    incomplete.pop("promotion_record_id")
    with pytest.raises(ContractError, match="requires promotion_record_id"):
        migrate_mapping("campaign_outcome", incomplete)


def test_mapping_migration_rejects_mismatched_and_future_versions():
    with pytest.raises(ContractError, match="does not match embedded"):
        migrate_mapping("campaign_outcome", {"schema_version": 1, **OUTCOME_V0}, source_version=0)
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
