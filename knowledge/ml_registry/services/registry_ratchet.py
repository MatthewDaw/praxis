from __future__ import annotations

import hashlib
import json
from typing import Any

from knowledge.ml_registry.domain.run import RunMetrics, RunValidity
from knowledge.ml_registry.storage.registry import Registry, RegistryError
from knowledge.ml_registry.schema import IDEA
from knowledge.ml_registry.write_path import RegistrySpace

from .registry_aliases import invalidate_adoption, record_ratchet_evidence


RATCHET_STREAK_LENGTH = 3
FAIRNESS_SIGNATURE_FIELDS = (
    "dataset_digest", "split_digest", "seed", "harness_digest", "preprocessing_digest",
)


def consider_rejection(
    registry: Registry,
    *,
    run_id: str,
    model_id: str,
    counterfactual_run_id: str,
    intervention_digest: str,
) -> bool:
    """Record paired evidence and atomically roll back a harmful active adoption.

    The observed run must evaluate the active champion version; its pair must evaluate
    that version's direct parent under the identical intervention.  Only fair, untampered
    pairs whose active-lineage metric is worse by more than the registered noise floor
    join the consecutive distinct-idea streak.
    """
    runs = {row["run_id"]: row for row in registry.rows("runs")}
    observed = runs.get(run_id)
    paired = runs.get(counterfactual_run_id)
    if observed is None or paired is None:
        raise RegistryError("paired ratchet evidence requires both registry runs")
    prior = [event for event in registry.list_events()
             if event.event_type == "ratchet_evidence_recorded"
             and event.payload.get("run_id") == run_id
             and event.payload.get("counterfactual_run_id") == counterfactual_run_id]
    if prior:
        payload = dict(prior[-1].payload)
        if (payload.get("intervention_digest") != intervention_digest
                or payload.get("evidence_digest") != _evidence_digest(observed, paired, intervention_digest)):
            raise RegistryError("ratchet evidence retry drifted from its full semantic payload")
        record_ratchet_evidence(registry, payload)
        return any(
            event.event_type == "adoption_invalidated"
            and run_id in event.payload.get("evidence_run_ids", ())
            for event in registry.list_events()
        )
    if observed["status"] != "succeeded" or observed["verdict"] != "rejected":
        raise RegistryError("ratchet evidence requires a fairly adjudicated rejected run")
    if paired["status"] != "complete" or paired["verdict"] is not None:
        raise RegistryError("counterfactual evidence must be a complete unadjudicated run")
    if observed["experiment_id"] != paired["experiment_id"]:
        raise RegistryError("paired ratchet runs must belong to the same experiment")

    alias = _one(registry.rows("aliases"), "model_id", model_id, "champion alias",
                 predicate=lambda row: row["alias"] == "champion")
    active_version = int(alias["version"])
    active = _one(registry.rows("model_versions"), "version", active_version, "active model version",
                  predicate=lambda row: row["model_id"] == model_id)
    parents = [row for row in registry.rows("lineage")
               if row["child_model_id"] == model_id and row["child_version"] == active_version
               and row["parent_model_id"] == model_id and row["kind"] == "derived_from"]
    if len(parents) != 1:
        raise RegistryError("active adoption must have exactly one direct parent version")
    parent_version = int(parents[0]["parent_version"])

    observed_params = json.loads(observed["params"])
    paired_params = json.loads(paired["params"])
    _validate_binding(observed_params, active_version, intervention_digest, "observed")
    _validate_binding(paired_params, parent_version, intervention_digest, "counterfactual")
    _validate_fairness_signature(observed_params, paired_params)
    if observed["idea_id"] != paired["idea_id"]:
        raise RegistryError("paired ratchet runs must measure the same idea")

    observed_metrics = RunMetrics.from_mapping(json.loads(observed["metrics"]))
    paired_metrics = RunMetrics.from_mapping(json.loads(paired["metrics"]))
    experiment = _one(registry.rows("experiments"), "experiment_id", observed["experiment_id"], "experiment")
    _validate_fair_pair(observed_metrics, paired_metrics, observed, paired,
                        float(experiment["baseline_throughput"]))
    delta = observed_metrics.metric - paired_metrics.metric
    harm = -delta if experiment["direction"] == "maximize" else delta
    evidence = {
        "model_id": model_id, "active_version": active_version, "parent_version": parent_version,
        "run_id": run_id, "counterfactual_run_id": counterfactual_run_id,
        "idea_id": observed["idea_id"], "intervention_digest": intervention_digest,
        "evidence_digest": _evidence_digest(observed, paired, intervention_digest),
        "harmful": harm > float(experiment["rope"]),
        "fairness_signature": {key: observed_params[key] for key in FAIRNESS_SIGNATURE_FIELDS},
    }
    record_ratchet_evidence(registry, evidence)
    if not evidence["harmful"]:
        return False
    streak = _current_streak(registry, model_id, active_version)
    if len(streak) < RATCHET_STREAK_LENGTH:
        return False
    adoption_run = runs[active["run_id"]]
    lineage_id = f"{model_id}@{active_version}"
    affected_ideas = sorted({
        str(row["idea_id"])
        for row in runs.values()
        if row["status"] == "succeeded" and row["verdict"] == "rejected"
        and json.loads(row["params"]).get("rejected_under_lineage_id") == lineage_id
    })
    invalidate_adoption(registry, {
        "model_id": model_id, "invalidated_version": active_version,
        "parent_version": parent_version, "adoption_run_id": adoption_run["run_id"],
        "evidence_run_ids": [item["run_id"] for item in streak[-RATCHET_STREAK_LENGTH:]],
        "invalidated_lineage_id": lineage_id, "requeue_idea_ids": affected_ideas,
        "reason": f"ratchet: {RATCHET_STREAK_LENGTH} consecutive distinct fairly measured paired rejections",
    })
    return True


def reconcile_registry_space_requeue(
    registry: Registry, space: RegistrySpace, *, event_sequence: int,
) -> dict[str, object]:
    """Apply a durable rollback event to the separate JSON RegistrySpace idempotently.

    SQLite and RegistrySpace are intentionally not presented as one transaction.  A caller
    can replay this projection after a crash; only ideas explicitly named by the event and
    still stamped as rejected in that exact lineage are reopened.
    """
    matches = [event for event in registry.list_events()
               if event.sequence == event_sequence and event.event_type == "adoption_invalidated"]
    if len(matches) != 1:
        raise RegistryError("requeue reconciliation requires one adoption_invalidated event sequence")
    payload = matches[0].payload
    lineage_id = payload["invalidated_lineage_id"]
    intended = tuple(str(item) for item in payload["requeue_idea_ids"])
    changed: list[str] = []
    for idea_id in intended:
        idea = space.get(idea_id)
        if idea is None or idea.category != IDEA:
            continue
        if idea.meta.get("registry_requeue_event_sequence") == event_sequence:
            continue
        if idea.meta.get("rejected_under_lineage_id") != lineage_id:
            continue
        for key in ("status", "rejection_reason", "rejected_under_adoption", "rejected_under_lineage_id"):
            idea.meta.pop(key, None)
        idea.meta["registry_requeue_event_sequence"] = event_sequence
        changed.append(idea_id)
    return {"event_sequence": event_sequence, "requeue_idea_ids": intended,
            "newly_requeued_idea_ids": tuple(changed)}


def _validate_binding(params: dict[str, Any], version: int, digest: str, noun: str) -> None:
    if params.get("evaluated_model_version") != version:
        raise RegistryError(f"{noun} evidence is not bound to the required model version")
    if not digest or params.get("intervention_digest") != digest:
        raise RegistryError(f"{noun} intervention digest is missing or mismatched")


def _validate_fairness_signature(observed: dict[str, Any], paired: dict[str, Any]) -> None:
    for field in FAIRNESS_SIGNATURE_FIELDS:
        if observed.get(field) in (None, "") or paired.get(field) in (None, ""):
            raise RegistryError(f"paired counterfactual requires explicit fairness signature field {field}")
        if observed[field] != paired[field]:
            raise RegistryError(f"paired counterfactual fairness signature differs at {field}")


def _validate_fair_pair(observed: RunMetrics, paired: RunMetrics,
                        observed_run: dict[str, Any], paired_run: dict[str, Any],
                        throughput_floor: float) -> None:
    if observed.validity is not RunValidity.VALID or paired.validity is not RunValidity.VALID:
        raise RegistryError("unfair counterfactual evidence cannot enter the ratchet")
    if observed.throughput_unit is not paired.throughput_unit:
        raise RegistryError("paired counterfactual throughput units are incomparable")
    if observed.throughput < throughput_floor or paired.throughput < throughput_floor:
        raise RegistryError("unfair counterfactual evidence cannot enter the ratchet")
    if observed_run["device_fingerprint"] != paired_run["device_fingerprint"]:
        raise RegistryError("paired counterfactual device fingerprints differ")


def _current_streak(registry: Registry, model_id: str, version: int) -> list[dict[str, Any]]:
    streak: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in reversed(registry.list_events()):
        if event.event_type == "adoption_invalidated" and event.payload.get("model_id") == model_id:
            break
        if event.event_type != "ratchet_evidence_recorded":
            continue
        payload = event.payload
        if payload.get("model_id") != model_id or payload.get("active_version") != version:
            continue
        if payload.get("harmful") is not True:
            break
        idea_id = str(payload["idea_id"])
        if idea_id in seen:
            break
        seen.add(idea_id)
        streak.append(dict(payload))
    streak.reverse()
    return streak


def _evidence_digest(observed: dict[str, Any], paired: dict[str, Any], intervention: str) -> str:
    raw = json.dumps({"observed": observed, "paired": paired, "intervention": intervention},
                     sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _one(rows: list[dict[str, Any]], field: str, value: object, noun: str, *, predicate=lambda _row: True):
    matches = [row for row in rows if row[field] == value and predicate(row)]
    if len(matches) != 1:
        raise RegistryError(f"unknown or ambiguous {noun}")
    return matches[0]
