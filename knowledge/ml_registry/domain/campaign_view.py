"""Read-only bridge vocabulary between Praxis IDEA facts and registry runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from knowledge.ml_registry.write_path import Fact


@dataclass(frozen=True)
class CampaignBinding:
    experiment_id: str
    model_id: str
    model_fact_id: str


@dataclass(frozen=True)
class IdeaInventory:
    fact: Fact
    display_id: str
    stage: str
    depends_on: tuple[str, ...]
    runs: tuple[Mapping[str, Any], ...]

    @property
    def fact_id(self) -> str:
        return self.fact.id


@dataclass(frozen=True)
class CampaignView:
    binding: CampaignBinding
    experiment: Mapping[str, Any]
    registered_model: Mapping[str, Any]
    model_fact: Fact
    ideas: tuple[IdeaInventory, ...]

    @property
    def runs(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(run for idea in self.ideas for run in idea.runs)
