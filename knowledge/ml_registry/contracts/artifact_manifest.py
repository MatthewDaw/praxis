from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from ._validation import ContractError, exact_keys, integer, text


@dataclass(frozen=True)
class CampaignArtifact:
    schema_version: int
    artifact_id: str
    artifact_type: str
    uri: str
    sha256: str
    size_bytes: int
    producer_campaign_id: str
    trial_id: str
    lineage_id: str
    interface_version: str

    VERSION = 1

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CampaignArtifact":
        exact_keys(value, set(cls.__dataclass_fields__), "campaign artifact")
        version = integer(value.get("schema_version"), "schema_version", minimum=1)
        if version != cls.VERSION:
            raise ContractError(f"unsupported CampaignArtifact schema_version {version}")
        digest = text(value.get("sha256"), "sha256").lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ContractError("sha256 must be 64 lowercase hexadecimal characters")
        return cls(version, *(text(value.get(name), name) for name in (
            "artifact_id", "artifact_type", "uri")), digest,
            integer(value.get("size_bytes"), "size_bytes"),
            *(text(value.get(name), name) for name in (
                "producer_campaign_id", "trial_id", "lineage_id", "interface_version")))

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)
