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

    @property
    def reason(self) -> str:
        return {
            self.ADVANCED: "a material family cleared the rope",
            self.STAGNANT: "all material families ran; none cleared the rope",
            self.VACUOUS: "stage has no material families",
        }[self]

    @classmethod
    def for_stage(
        cls, *, material_families: int, completed_families: int, advanced: bool,
        forced: bool = False,
    ) -> "StageOutcome":
        """The stage's terminal outcome, or a refusal while it is still open.

        ``forced`` is the caller asserting the stage is being closed EARLY by policy rather
        than by exhaustion -- the stagnation bound is the one such policy (see
        :meth:`StageCloseRecord.for_stagnation`). A forced close still has to be a real
        outcome: it maps a partial, non-advanced stage to :data:`STAGNANT` instead of a
        refusal, and changes nothing else.
        """
        material_families = integer(material_families, "material_families", minimum=0)
        completed_families = integer(completed_families, "completed_families", minimum=0)
        if completed_families > material_families:
            raise ContractError("completed stage families cannot exceed material families")
        if not isinstance(advanced, bool):
            raise ContractError("advanced must be boolean")
        if material_families == 0:
            return cls.VACUOUS
        if advanced:
            return cls.ADVANCED
        if completed_families == material_families or forced:
            return cls.STAGNANT
        raise ContractError("stage is still open and has no terminal outcome")


@dataclass(frozen=True)
class StageCloseRecord:
    """How one stage ended: its typed :class:`StageOutcome`, ONE canonical reason, and the
    counts that reason was drawn from, kept as separate detail rather than folded into prose.

    This is the persisted shape of a stage close. A close written as a bare dictionary carried
    no ``outcome`` at all, so the same stagnant stage had two incompatible representations --
    the supervisor's and this module's -- and neither could validate the other.
    """

    stage: str
    outcome: StageOutcome
    reason: str
    experiments_without_improvement: int
    limit: int

    @classmethod
    def for_stagnation(
        cls, *, stage: str, material_families: int, completed_families: int,
        experiments_without_improvement: int, limit: int,
    ) -> "StageCloseRecord":
        """A stage closed EARLY by its no-improvement bound rather than by exhaustion.

        The bound can fire with families still untried -- which :meth:`StageOutcome.for_stage`
        would otherwise refuse as an open stage -- so the close is forced. Because it is forced,
        the enum's own STAGNANT reason ("all material families ran") may be false here, and the
        reason authored instead says how many families never ran.
        """
        limit_value = integer(limit, "limit", minimum=1)
        count = integer(experiments_without_improvement, "experiments_without_improvement")
        if count < limit_value:
            raise ContractError(f"a stagnation close needs {limit_value} experiments without "
                                f"an improvement, not {count}")
        outcome = StageOutcome.for_stage(material_families=material_families,
                                         completed_families=completed_families,
                                         advanced=False, forced=True)
        if outcome is not StageOutcome.STAGNANT:
            raise ContractError(f"a stagnation close needs material families, got {outcome.value}")
        untried = material_families - completed_families
        return cls(text(stage, "stage"), outcome,
                   f"no improvement in the last {limit_value} experiments; "
                   f"{untried} of {material_families} material families untried",
                   count, limit_value)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StageCloseRecord":
        exact_keys(value, set(cls.__dataclass_fields__), "stage close")
        try:
            outcome = StageOutcome(value.get("outcome"))
        except ValueError as exc:
            raise ContractError(f"unknown stage outcome {value.get('outcome')!r}") from exc
        return cls(text(value.get("stage"), "stage"), outcome, text(value.get("reason"), "reason"),
                   integer(value.get("experiments_without_improvement"),
                           "experiments_without_improvement"),
                   integer(value.get("limit"), "limit", minimum=1))

    def to_mapping(self) -> dict[str, Any]:
        return {**asdict(self), "outcome": self.outcome.value}


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
