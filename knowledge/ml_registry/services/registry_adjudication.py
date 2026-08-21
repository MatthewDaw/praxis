from __future__ import annotations

import json
from typing import Any, Mapping

from knowledge.ml_registry.domain.run import RunMetrics, RunValidity
from knowledge.ml_registry.storage.registry import Registry, RegistryError

from .registry_aliases import adjudicate_run, adopt_run_and_promote


def adjudicate_against_champion(
    registry: Registry,
    *,
    run_id: str,
    model_id: str,
    reason: str,
    promotion: Mapping[str, Any] | None = None,
) -> str:
    """Derive a verdict from canonical registry state and, for a win, promote its version.

    The trainer supplies measurements only. The current champion supplies the comparison
    baseline; callers cannot assert either a verdict or a comparison value.
    """
    run = _one(registry.rows("runs"), "run_id", run_id, "run")
    if run["status"] == "succeeded" and run["verdict"] == "adopted":
        if promotion is None:
            raise RegistryError("an adopted run retry requires its full promotion inputs")
        values = dict(promotion)
        values["run_id"] = run_id
        values["model_id"] = model_id
        adopt_run_and_promote(registry, run_id=run_id, model_id=model_id, reason=reason,
                              model_version=values)
        return "adopted"
    if run["status"] != "complete" or run["verdict"] is not None:
        raise RegistryError("adjudication requires one complete, unadjudicated run")
    experiment = _one(registry.rows("experiments"), "experiment_id", run["experiment_id"], "experiment")
    alias = next((row for row in registry.rows("aliases")
                  if row["model_id"] == model_id and row["alias"] == "champion"), None)
    if alias is None:
        raise RegistryError("registry-native adjudication requires a current champion baseline")
    champion_version = next((row for row in registry.rows("model_versions")
                             if row["model_id"] == model_id and row["version"] == alias["version"]), None)
    if champion_version is None:
        raise RegistryError("champion alias references an unknown model version")
    champion_run = _one(registry.rows("runs"), "run_id", champion_version["run_id"], "champion run")
    if champion_run["experiment_id"] != run["experiment_id"]:
        raise RegistryError("champion baseline belongs to a different experiment")
    candidate = RunMetrics.from_mapping(json.loads(run["metrics"]))
    baseline = RunMetrics.from_mapping(json.loads(champion_run["metrics"]))

    if candidate.validity is RunValidity.INVALID:
        verdict, status = "voided", "voided"
    elif candidate.throughput_unit is not baseline.throughput_unit:
        raise RegistryError("candidate and champion throughput units are incomparable")
    elif candidate.throughput < float(experiment["baseline_throughput"]):
        verdict, status = "voided", "voided"
    else:
        delta = candidate.metric - baseline.metric
        improvement = delta if experiment["direction"] == "maximize" else -delta
        floor = float(experiment["noise_floor"])
        if improvement > floor:
            verdict, status = "adopted", "succeeded"
        elif abs(delta) <= floor:
            verdict, status = "parked", "succeeded"
        else:
            verdict, status = "rejected", "succeeded"

    if verdict == "adopted" and promotion is None:
        raise RegistryError("an adopted run requires artifact and compatibility inputs for champion promotion")
    if verdict == "adopted":
        _validate_promotion_inputs(registry, run_id, model_id, promotion or {})
    if verdict == "adopted":
        values = dict(promotion or {})
        if values.get("run_id", run_id) != run_id or values.get("model_id", model_id) != model_id:
            raise RegistryError("promotion inputs must name the adjudicated run and model")
        values["run_id"] = run_id
        values["model_id"] = model_id
        adopt_run_and_promote(registry, run_id=run_id, model_id=model_id, reason=reason,
                              model_version=values)
    else:
        adjudicate_run(registry, run_id=run_id, verdict=verdict, status=status, reason=reason)
    return verdict


def _validate_promotion_inputs(registry: Registry, run_id: str, model_id: str,
                               promotion: Mapping[str, Any]) -> None:
    required = {"version", "artifact_id", "checksum", "family_version", "code_sha",
                "preprocessing_hash", "calibration", "thresholds", "compat_result", "status"}
    missing = required - set(promotion)
    if missing:
        raise RegistryError(f"champion promotion is missing inputs: {sorted(missing)}")
    if promotion.get("run_id", run_id) != run_id or promotion.get("model_id", model_id) != model_id:
        raise RegistryError("promotion inputs must name the adjudicated run and model")
    artifact_id = str(promotion["artifact_id"])
    if promotion["checksum"] != artifact_id or not any(
        row["artifact_id"] == artifact_id and row["run_id"] == run_id
        for row in registry.rows("artifacts")
    ):
        raise RegistryError("champion promotion requires the adjudicated run's checksummed artifact")
    compat = promotion["compat_result"]
    if not isinstance(compat, Mapping) or set(compat) != {"head_sha", "passed", "at"} or compat["passed"] is not True:
        raise RegistryError("champion promotion requires passing compatibility inputs")


def _one(rows: list[dict[str, Any]], field: str, value: object, noun: str) -> dict[str, Any]:
    match = next((row for row in rows if row[field] == value), None)
    if match is None:
        raise RegistryError(f"unknown {noun}")
    return match
