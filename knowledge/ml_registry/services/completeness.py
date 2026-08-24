"""Registry-native campaign completion over the strict :class:`CampaignView` bridge."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from typing import Any

from knowledge.ml_registry.contracts import StageOutcome
from knowledge.ml_registry.domain import CampaignView
from knowledge.ml_registry.domain.status import answers_question, fairly_measured, retryable
from knowledge.ml_registry.services.campaign_view import STANDARD_TABLES
from knowledge.ml_registry.storage.registry import Registry, RegistryError


def campaign_completeness(
    view: CampaignView, registry: Registry, *, min_measured: int = 3,
) -> dict[str, Any]:
    """Return complete stage coverage and every registry-native blocker.

    IDEA facts remain authoritative for membership and dependencies.  Experiment
    rows own the ordered stage vocabulary; registry runs own execution status and
    verdicts.  Only the latest run for an idea contributes to coverage.
    """
    result = campaign_coverage(view, registry, min_measured=min_measured)
    production = _production_blocker(view, registry, _latest_by_idea(view))
    if production is not None:
        result["blocking"].append(production)
    result["done"] = not result["blocking"]
    return result


def campaign_coverage(
    view: CampaignView, registry: Registry, *, min_measured: int = 3,
) -> dict[str, Any]:
    """Project stage coverage without making a terminal completion claim.

    Adjudication and finalization may inspect this projection before a production
    alias exists.  Callers deciding whether a campaign is done must use
    :func:`campaign_completeness`, which cannot waive production proof.
    """
    if frozenset(registry.table_names()) != STANDARD_TABLES:
        raise RegistryError("campaign coverage requires the exact eight-table standard registry")
    if min_measured < 1:
        raise ValueError("min_measured must be positive")

    stages = tuple(json.loads(view.experiment["stages"]))
    ideas = {idea.fact_id: idea for idea in view.ideas}
    latest = _latest_by_idea(view)
    answered = {idea_id for idea_id, run in latest.items() if run and _answers(run)}
    adopted = {idea_id for idea_id, run in latest.items()
               if run and run["status"] == "succeeded" and run["verdict"] == "adopted"}
    unreachable = _dependency_blocked(ideas, answered, adopted)
    closed = answered | unreachable

    coverage: list[dict[str, Any]] = []
    blocking: list[dict[str, str]] = []
    for stage in stages:
        members = [idea for idea in view.ideas if idea.stage == stage]
        measured = [idea for idea in members
                    if latest[idea.fact_id] and _fair_measurement(latest[idea.fact_id])]
        open_ideas = [idea for idea in members if idea.fact_id not in closed]
        advanced = any(idea.fact_id in adopted for idea in members)
        outcome: StageOutcome | None = None
        reason: str | None = None
        if not open_ideas and (not members or len(measured) >= min_measured):
            outcome = StageOutcome.for_stage(
                material_families=len(members),
                completed_families=len(members),
                advanced=advanced,
            )
            reason = ("a material family cleared the rope" if advanced
                      else "stage has no material families" if not members
                      else "all material families ran; none cleared the rope")
        row = {"stage": stage, "total": len(members), "measured": len(measured),
               "closed": not open_ideas, "thin": bool(members) and not open_ideas
               and len(measured) < min_measured,
               "outcome": None if outcome is None else outcome.value, "reason": reason}
        coverage.append(row)
        if open_ideas:
            blocking.append(_blocker("stage_open", stage,
                                     f"{len(open_ideas)} idea(s) remain unanswered in {stage!r}"))
        elif members and len(measured) < min_measured:
            blocking.append(_blocker("stage_thin", stage,
                                     f"{stage!r} closed on {len(measured)} fair measurement(s), "
                                     f"below the floor of {min_measured}"))

    reruns = sorted(idea.display_id for idea in view.ideas
                    if latest[idea.fact_id] and retryable(latest[idea.fact_id]["status"]))
    if reruns:
        blocking.append(_blocker("awaiting_rerun", "",
                                 "retryable ideas are unmeasured: " + ", ".join(reruns)))
    return {"experiment_id": view.binding.experiment_id, "model_id": view.binding.model_id,
            "done": not blocking, "blocking": blocking, "coverage": coverage}


def _latest_by_idea(view: CampaignView) -> dict[str, Mapping[str, Any] | None]:
    return {idea.fact_id: _latest(idea.runs) for idea in view.ideas}


def _latest(runs: tuple[Mapping[str, Any], ...]) -> Mapping[str, Any] | None:
    return max(runs, key=lambda run: (run["started_at"], run["run_id"]), default=None)


def _answers(run: Mapping[str, Any]) -> bool:
    return answers_question(run["status"]) and run["verdict"] in {"adopted", "rejected", "parked"}


def _fair_measurement(run: Mapping[str, Any]) -> bool:
    if not fairly_measured(run["status"]) or run["verdict"] not in {"adopted", "rejected", "parked"}:
        return False
    params = json.loads(run["params"]) if isinstance(run["params"], str) else run["params"]
    if params.get("incumbent_remeasurement") is True:
        return False
    resolved, incumbent = params.get("resolved_configuration"), params.get("incumbent_configuration")
    return resolved is None or incumbent is None or resolved != incumbent


def _dependency_blocked(ideas: Mapping[str, Any], answered: set[str], adopted: set[str]) -> set[str]:
    """Ideas whose answered dependency can no longer be adopted cannot hold a stage open."""
    result: set[str] = set()
    changed = True
    while changed:
        changed = False
        for idea_id, idea in ideas.items():
            if idea_id in answered or idea_id in result:
                continue
            if any(dep in answered | result and dep not in adopted for dep in idea.depends_on):
                result.add(idea_id)
                changed = True
    return result


def _production_blocker(view: CampaignView, registry: Registry,
                        latest: Mapping[str, Mapping[str, Any] | None]) -> dict[str, str] | None:
    aliases = [row for row in registry.rows("aliases")
               if row["model_id"] == view.binding.model_id and row["alias"] == "production"]
    if not aliases:
        return _blocker("no_production_alias", "", "registered model has no production alias")
    alias = aliases[0]
    champions = [row for row in registry.rows("aliases")
                 if row["model_id"] == view.binding.model_id and row["alias"] == "champion"]
    if not champions or champions[0]["version"] != alias["version"]:
        return _blocker("wrong_production_lineage", "",
                        "production version is not the current champion version")
    try:
        version = registry.effective_model_version(view.binding.model_id, alias["version"])
    except RegistryError as exc:
        return _blocker("invalid_production_alias", "", str(exc))
    if version["effective_status"] != "active" or not version["effective_compat_result"]["passed"]:
        return _blocker("incompatible_production", "", "production version is not effectively active and compatible")

    runs = {run["run_id"]: run for run in view.runs}
    run = runs.get(version["run_id"])
    if run is None or run["status"] != "succeeded" or run["verdict"] != "adopted":
        return _blocker("wrong_production_lineage", "", "production version is not bound to an adopted campaign run")
    current_adoptions = [candidate for candidate in latest.values()
                         if candidate and candidate["status"] == "succeeded"
                         and candidate["verdict"] == "adopted"]
    current = max(current_adoptions, key=lambda item: (item["finished_at"] or item["started_at"],
                                                       item["run_id"]), default=None)
    if current is None or current["run_id"] != run["run_id"]:
        return _blocker("wrong_production_lineage", "", "production version is not the current adopted run")

    artifact_rows = [row for row in registry.rows("artifacts")
                     if row["artifact_id"] == version["artifact_id"] and row["run_id"] == run["run_id"]]
    if not artifact_rows or version["checksum"] != version["artifact_id"]:
        return _blocker("stale_production_artifact", "", "production artifact or checksum binding is missing")
    try:
        registry.blobs.verify(version["artifact_id"])
    except Exception as exc:
        return _blocker("stale_production_artifact", "", f"production artifact verification failed: {exc}")

    code_ref = json.loads(run["code_ref"]) if isinstance(run["code_ref"], str) else run["code_ref"]
    try:
        head = subprocess.run(["git", "-C", code_ref["repo"], "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        return _blocker("stale_production_code", "", f"cannot resolve current HEAD: {exc}")
    compat = version["effective_compat_result"]
    if version["code_sha"] != code_ref["sha"] or compat["head_sha"] != head:
        return _blocker("stale_production_code", "", "production compatibility is not verified for current HEAD")
    return None


def _blocker(kind: str, stage: str, detail: str) -> dict[str, str]:
    return {"kind": kind, "stage": stage, "detail": detail}
