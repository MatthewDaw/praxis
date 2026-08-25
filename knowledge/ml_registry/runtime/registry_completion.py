"""Controller adapter for the canonical RegistryFinalizer completion authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from knowledge.ml_registry.domain import CampaignView
from knowledge.ml_registry.contracts import CampaignOutcome, CampaignOutcomeRecord
from knowledge.ml_registry.services.registry_finalize import RegistryFinalizer


@dataclass(frozen=True)
class CampaignFinalization:
    view: CampaignView
    finalizer: RegistryFinalizer
    version: int
    artifact_type: str


class RegistryCompletionVerifier:
    """Translate a verified production alias into the legacy portfolio projection."""

    def __init__(self, campaigns: Mapping[str, CampaignFinalization]) -> None:
        self.campaigns = dict(campaigns)
        self.verifications: dict[str, int] = {}

    def __call__(self, campaign_id: str, polled) -> dict[str, object]:
        if polled.artifact is None:
            raise ValueError("completion outcome is missing")
        outcome = CampaignOutcomeRecord.from_mapping(polled.artifact)
        if outcome.outcome not in {CampaignOutcome.COMPLETE, CampaignOutcome.PROMOTED}:
            raise ValueError("completion outcome is missing")
        binding = self.campaigns[campaign_id]
        finalized = binding.finalizer.verify(binding.view, version=binding.version)
        self.verifications[campaign_id] = self.verifications.get(campaign_id, 0) + 1
        version = finalized.model_version
        return {
            "artifact_id": binding.artifact_type,
            "model_id": version.model_id,
            "verdict": "adopted",
            "dataset_manifest_hash": version.checksum,
            "split_manifest_hash": version.preprocessing_hash,
            "prediction_manifest_hash": version.checksum,
            "coverage": 1.0,
            "input_artifact_ids": [],
        }
