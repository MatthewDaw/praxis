"""Pure, offline schema migrations for serialized campaign contracts.

Migrations never read or write campaign state. Callers provide a detached mapping
or ledger string and receive a new value suitable for validation by the current
contract type.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from ._validation import ContractError
from .artifact_manifest import CampaignArtifact
from .campaign_spec import CampaignSpec
from .launch_intent import LaunchIntent
from .lease import CampaignLease
from .ledger_v2 import LEDGER_V2_HEADER, LedgerV2
from .outcome import CampaignOutcomeRecord
from .promotion import PromotionRecord


LATEST_SCHEMA_VERSIONS = {
    "campaign_spec": CampaignSpec.VERSION,
    "campaign_artifact": CampaignArtifact.VERSION,
    "promotion_record": PromotionRecord.VERSION,
    "campaign_outcome": CampaignOutcomeRecord.VERSION,
    "launch_intent": LaunchIntent.VERSION,
    "campaign_lease": CampaignLease.VERSION,
    "ledger": 2,
}

_VALIDATORS = {
    "campaign_spec": CampaignSpec.from_mapping,
    "campaign_artifact": CampaignArtifact.from_mapping,
    "promotion_record": PromotionRecord.from_mapping,
    "campaign_outcome": CampaignOutcomeRecord.from_mapping,
    "launch_intent": LaunchIntent.from_mapping,
    "campaign_lease": CampaignLease.from_mapping,
}


def migrate_mapping(kind: str, payload: Mapping[str, Any], *, source_version: int | None = None) -> dict[str, Any]:
    """Return a current, validated copy of one object contract.

    Version 0 denotes the pre-contract fixture shape: the current fields were
    already present but ``schema_version`` was absent. This migration adds only
    that discriminator. It deliberately provides no aliases or inferred data.
    """
    if kind not in _VALIDATORS:
        raise ContractError(f"unknown contract kind {kind!r}")
    if not isinstance(payload, Mapping):
        raise ContractError("migration payload must be an object")
    result = deepcopy(dict(payload))
    embedded = result.get("schema_version")
    if source_version is None:
        source_version = 0 if embedded is None else embedded
    if isinstance(source_version, bool) or not isinstance(source_version, int) or source_version < 0:
        raise ContractError("source_version must be a non-negative integer")
    if embedded is not None and embedded != source_version:
        raise ContractError(
            f"declared source_version {source_version} does not match embedded schema_version {embedded}"
        )
    target = LATEST_SCHEMA_VERSIONS[kind]
    if source_version > target:
        raise ContractError(f"cannot migrate future {kind} schema_version {source_version}")
    if source_version == 0:
        result["schema_version"] = 1
        source_version = 1
    if source_version != target:
        raise ContractError(f"no {kind} migration path from schema_version {source_version} to {target}")
    return _VALIDATORS[kind](result).to_mapping()


def migrate_ledger(content: str) -> str:
    """Validate LedgerV2 or refuse a lossy legacy-ledger conversion.

    V0/V1 omit throughput and diff_lines, two adjudication inputs that cannot be
    reconstructed. Their writer must be upgraded and new measurements emitted.
    """
    first_line = content.splitlines()[0] if content.splitlines() else ""
    header = tuple(first_line.split("\t"))
    if header != LEDGER_V2_HEADER:
        raise ContractError(
            "legacy ledger cannot be migrated offline: throughput and diff_lines must be emitted by the writer"
        )
    return LedgerV2.parse(content).serialize()
