from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping

from ._validation import ContractError, exact_keys, integer, text


class CampaignOutcome(str, Enum):
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"
    STALLED = "STALLED"
    RETRYABLE = "RETRYABLE"
    FAILED = "FAILED"
    QUOTA = "QUOTA"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class CampaignOutcomeRecord:
    schema_version: int
    campaign_id: str
    outcome: CampaignOutcome
    reason: str
    attempt: int
    promotion_record_id: str | None = None

    VERSION = 1

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CampaignOutcomeRecord":
        exact_keys(value, set(cls.__dataclass_fields__), "campaign outcome")
        version = integer(value.get("schema_version"), "schema_version", minimum=1)
        if version != cls.VERSION:
            raise ContractError(f"unsupported CampaignOutcome schema_version {version}")
        try:
            outcome = CampaignOutcome(value.get("outcome"))
        except ValueError as exc:
            raise ContractError(f"unknown campaign outcome {value.get('outcome')!r}") from exc
        promotion = value.get("promotion_record_id")
        if promotion is not None:
            promotion = text(promotion, "promotion_record_id")
        if outcome is CampaignOutcome.COMPLETE and promotion is None:
            raise ContractError("COMPLETE requires promotion_record_id")
        return cls(version, text(value.get("campaign_id"), "campaign_id"), outcome,
                   text(value.get("reason"), "reason"), integer(value.get("attempt"), "attempt", minimum=1),
                   promotion)

    def to_mapping(self) -> dict[str, Any]:
        result = asdict(self)
        result["outcome"] = self.outcome.value
        return result
