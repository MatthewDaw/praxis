"""Strict, read-only join of campaign facts to the standard registry."""

from __future__ import annotations

import json

from knowledge.ml_registry.domain.campaign_view import CampaignBinding, CampaignView, IdeaInventory
from knowledge.ml_registry.schema import IDEA, MODEL
from knowledge.ml_registry.storage.registry import Registry, RegistryError
from knowledge.ml_registry.write_path import RegistrySpace


STANDARD_TABLES = frozenset({
    "aliases", "artifacts", "events", "experiments", "lineage", "model_versions",
    "registered_models", "runs",
})


class CampaignViewError(RegistryError):
    pass


def build_campaign_view(
    space: RegistrySpace, registry: Registry, binding: CampaignBinding,
) -> CampaignView:
    """Join ``runs.idea_id`` to authoritative IDEA ``Fact.id`` values.

    This service never writes either store. Display tags in ``meta.id`` are labels,
    never join keys. Experiment stages are the sole stage vocabulary.
    """
    tables = frozenset(registry.table_names())
    if tables != STANDARD_TABLES:
        raise CampaignViewError(
            f"campaign view requires the exact eight-table standard registry; found {sorted(tables)!r}"
        )

    experiments = {row["experiment_id"]: row for row in registry.rows("experiments")}
    experiment = experiments.get(binding.experiment_id)
    if experiment is None:
        raise CampaignViewError(f"unknown experiment {binding.experiment_id!r}")
    models = {row["model_id"]: row for row in registry.rows("registered_models")}
    registered_model = models.get(binding.model_id)
    if registered_model is None:
        raise CampaignViewError(f"unknown registered model {binding.model_id!r}")
    model_fact = space.get(binding.model_fact_id)
    if model_fact is None or model_fact.category != MODEL:
        raise CampaignViewError(f"unknown model fact {binding.model_fact_id!r}")

    stages_raw = json.loads(experiment["stages"])
    if (not isinstance(stages_raw, list) or not stages_raw
            or any(not isinstance(stage, str) or not stage for stage in stages_raw)
            or len(stages_raw) != len(set(stages_raw))):
        raise CampaignViewError("experiment stages must be a non-empty ordered set of names")
    stages = tuple(stages_raw)

    ideas = [fact for fact in space.list_facts(IDEA)
             if fact.meta.get("model_id") == binding.model_fact_id]
    by_fact_id = {fact.id: fact for fact in ideas}
    display_ids: dict[str, list[str]] = {}
    for fact in ideas:
        display_ids.setdefault(str(fact.meta.get("id") or fact.id), []).append(fact.id)

    def canonical_dependency(reference: object, owner: str) -> str:
        ref = str(reference)
        if ref in by_fact_id:
            return ref
        matches = display_ids.get(ref, [])
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise CampaignViewError(f"idea {owner!r} has ambiguous dependency tag {ref!r}")
        foreign = space.get(ref)
        if foreign is not None:
            raise CampaignViewError(f"idea {owner!r} depends on foreign fact {ref!r}")
        raise CampaignViewError(f"idea {owner!r} has unknown dependency {ref!r}")

    inventories: dict[str, IdeaInventory] = {}
    for fact in ideas:
        stage = str(fact.meta.get("stage") or fact.meta.get("axis") or "")
        if stage not in stages:
            raise CampaignViewError(f"idea {fact.id!r} has unknown stage {stage!r}")
        dependencies = tuple(canonical_dependency(dep, fact.id)
                             for dep in (fact.meta.get("depends_on") or ()))
        inventories[fact.id] = IdeaInventory(
            fact=fact, display_id=str(fact.meta.get("id") or fact.id), stage=stage,
            depends_on=dependencies, runs=(),
        )

    campaign_runs = registry.list_runs(experiment_id=binding.experiment_id)
    all_runs = registry.list_runs()
    local_ids = set(inventories)
    for run in all_runs:
        if run["idea_id"] in local_ids and run["experiment_id"] != binding.experiment_id:
            raise CampaignViewError(
                f"foreign experiment {run['experiment_id']!r} references campaign idea {run['idea_id']!r}"
            )
    runs_by_idea: dict[str, list[dict]] = {fact_id: [] for fact_id in local_ids}
    for run in campaign_runs:
        idea = inventories.get(run["idea_id"])
        if idea is None:
            foreign = space.get(run["idea_id"])
            detail = "foreign" if foreign is not None else "orphan"
            raise CampaignViewError(f"{detail} run {run['run_id']!r} joins idea {run['idea_id']!r}")
        if run["stage"] not in stages:
            raise CampaignViewError(f"run {run['run_id']!r} has unknown stage {run['stage']!r}")
        if run["stage"] != idea.stage:
            raise CampaignViewError(
                f"run {run['run_id']!r} stage {run['stage']!r} differs from idea stage {idea.stage!r}"
            )
        runs_by_idea[idea.fact_id].append(run)

    ordered = []
    for fact in ideas:
        item = inventories[fact.id]
        ordered.append(IdeaInventory(item.fact, item.display_id, item.stage, item.depends_on,
                                     tuple(runs_by_idea[fact.id])))
    return CampaignView(binding, experiment, registered_model, model_fact, tuple(ordered))
