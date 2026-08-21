from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping

from ._validation import ContractError, exact_keys, integer, text
from .production_alias import ProductionAliasRef


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
    production_alias: ProductionAliasRef | None = None

    VERSION = 2

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
        production_raw = value.get("production_alias")
        production = (None if production_raw is None else
                      ProductionAliasRef.from_mapping(production_raw)
                      if isinstance(production_raw, Mapping) else None)
        if production_raw is not None and production is None:
            raise ContractError("production must be an object or null")
        if outcome is CampaignOutcome.COMPLETE and production is None:
            raise ContractError("COMPLETE requires a canonical production alias reference")
        return cls(version, text(value.get("campaign_id"), "campaign_id"), outcome,
                   text(value.get("reason"), "reason"), integer(value.get("attempt"), "attempt", minimum=1),
                   production)

    def to_mapping(self) -> dict[str, Any]:
        result = asdict(self)
        result["outcome"] = self.outcome.value
        result["production_alias"] = (None if self.production_alias is None
                                      else self.production_alias.to_mapping())
        return result
