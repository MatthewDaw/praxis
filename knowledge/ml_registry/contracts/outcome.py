from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping

from ._validation import ContractError, exact_keys, integer, text
from .production_alias import ProductionAliasRef


class CampaignOutcome(str, Enum):
    PROMOTED = "PROMOTED"
    MEASURED = "MEASURED"
    REFUTED = "REFUTED"
    ABANDONED = "ABANDONED"

    # Runtime transport outcomes retained for version-2 record compatibility.
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"
    STALLED = "STALLED"
    RETRYABLE = "RETRYABLE"
    FAILED = "FAILED"
    QUOTA = "QUOTA"
    CANCELLED = "CANCELLED"


class StageOutcome(str, Enum):
    ADVANCED = "ADVANCED"
    STAGNANT = "STAGNANT"
    VACUOUS = "VACUOUS"

    @classmethod
    def for_stage(
        cls, *, material_families: int, completed_families: int, advanced: bool,
    ) -> "StageOutcome":
        counts = (material_families, completed_families)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
            raise ContractError("stage family counts must be non-negative integers")
        if completed_families > material_families:
            raise ContractError("completed stage families cannot exceed material families")
        if not isinstance(advanced, bool):
            raise ContractError("advanced must be boolean")
        if material_families == 0:
            return cls.VACUOUS
        if advanced:
            return cls.ADVANCED
        if completed_families == material_families:
            return cls.STAGNANT
        raise ContractError("stage is still open and has no terminal outcome")


@dataclass(frozen=True)
class CampaignOutcomeRecord:
    schema_version: int
    campaign_id: str
    outcome: CampaignOutcome
    reason: str
    attempt: int
    production_alias: ProductionAliasRef | None = None

    VERSION = 2

    def __post_init__(self) -> None:
        text(self.reason, "reason")
        integer(self.attempt, "attempt", minimum=1)
        if self.outcome in {CampaignOutcome.COMPLETE, CampaignOutcome.PROMOTED} \
                and self.production_alias is None:
            raise ContractError(f"{self.outcome.value} requires a canonical production alias reference")

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
        return cls(version, text(value.get("campaign_id"), "campaign_id"), outcome,
                   text(value.get("reason"), "reason"), integer(value.get("attempt"), "attempt", minimum=1),
                   production)

    def to_mapping(self) -> dict[str, Any]:
        result = asdict(self)
        result["outcome"] = self.outcome.value
        result["production_alias"] = (None if self.production_alias is None
                                      else self.production_alias.to_mapping())
        return result
