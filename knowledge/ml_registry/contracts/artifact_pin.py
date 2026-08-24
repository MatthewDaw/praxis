from __future__ import annotations

from dataclasses import dataclass
@dataclass(frozen=True)
class ArtifactPin:
    """Concrete immutable producer version resolved before a consumer is claimed."""

    producer_campaign_id: str
    artifact_type: str
    model_id: str
    version: int
    artifact_id: str

    def to_mapping(self) -> dict[str, str | int]:
        return {
            "producer_campaign_id": self.producer_campaign_id,
            "artifact_type": self.artifact_type,
            "model_id": self.model_id,
            "version": self.version,
            "artifact_id": self.artifact_id,
        }
