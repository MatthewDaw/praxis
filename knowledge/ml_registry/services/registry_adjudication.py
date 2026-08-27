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
    counterfactual_run_id: str | None = None,
    intervention_digest: str | None = None,
    paired_evidence: Mapping[str, object] | None = None,
) -> str:
    """Derive a verdict from canonical registry state and, for a win, promote its version.

    The trainer supplies measurements only. The current champion supplies the comparison
    baseline; callers cannot assert either a verdict or a comparison value.
    """
    if (counterfactual_run_id is None) != (intervention_digest is None):
        raise RegistryError("ratchet evidence requires both counterfactual_run_id and intervention_digest")
    run = _one(registry.rows("runs"), "run_id", run_id, "run")
    if run["status"] == "succeeded" and run["verdict"] == "adopted":
        if promotion is None:
            raise RegistryError("an adopted run retry requires its full promotion inputs")
        prior = next(
            event for event in reversed(registry.list_events())
            if event.event_type == "run_adopted" and event.payload.get("run_id") == run_id
        )
        stored_evidence = prior.payload.get("adjudication_evidence")
        if paired_evidence is not None:
            from .paired_adjudication import evidence_digest

            if not isinstance(stored_evidence, Mapping) or (
                evidence_digest(paired_evidence) != stored_evidence.get("input_sha256")
            ):
                raise RegistryError("atomic adoption retry drifted from its paired evidence")
        values = dict(promotion)
        values["run_id"] = run_id
        values["model_id"] = model_id
        adopt_run_and_promote(registry, run_id=run_id, model_id=model_id, reason=reason,
                              model_version=values, adjudication_evidence=stored_evidence)
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

    adjudication_evidence: Mapping[str, object] | None = None
    if candidate.validity is RunValidity.INVALID:
        verdict, status = "voided", "voided"
    elif candidate.throughput_unit is not baseline.throughput_unit:
        raise RegistryError("candidate and champion throughput units are incomparable")
    elif candidate.throughput < float(experiment["baseline_throughput"]):
        verdict, status = "voided", "voided"
    else:
        from knowledge.ml_registry.floor import (
            FLOOR_ADOPTION_INSIDE_ROPE_FIELD,
            FLOOR_ADOPTION_UNSUPPORTED_BY_INTERVAL_FIELD,
            adoption_gain,
            clears_adoption_floor,
        )

        from .paired_adjudication import (
            LEGACY_SCALAR_ROPE,
            PAIRED_BOOTSTRAP,
            campaign_metric,
            comparison_policy,
            paired_interval,
        )

        # THE ADOPTION FLOOR ON THE CANONICAL PATH. A gain of `adoption_floor` or more in
        # absolute metric points (0.5% by default, declared with the judge) IS a win and is
        # adopted outright; the interval, or the scalar rope, decides only what falls below
        # it. Same rule, same reader, same default as `verdict.adjudicate_verdict` -- the
        # Praxis-space adjudication -- so the two cannot answer one question two ways.
        #
        # WHERE THE NUMBER COMES FROM, and it is one place: the registered CampaignSpec's
        # `metric` object, frozen before any run of this campaign, read by
        # `floor.declared_adoption_floor`. `campaign_metric` also VALIDATES it, so an
        # unusable floor is refused rather than silently replaced by the default. An
        # experiment with no CampaignSpec at all (a historical import) has no declaration to
        # read and takes the documented default, exactly as an undeclared model does on the
        # other path -- a default, not a second source.
        #
        # DIRECTION lives in `adoption_gain` and nowhere else here: on a `minimize` metric a
        # floor-sized REGRESSION is a gain of -0.005 and clears nothing.
        spec_metric = campaign_metric(registry, str(experiment["experiment_id"]))
        gain = adoption_gain(str(experiment["direction"]), baseline.metric, candidate.metric)
        adopted_by_floor = clears_adoption_floor(dict(spec_metric or {}), gain)

        policy = comparison_policy(registry, str(experiment["experiment_id"]))
        method = LEGACY_SCALAR_ROPE if policy is None else policy.get("method")
        if method == PAIRED_BOOTSTRAP:
            if paired_evidence is None:
                raise RegistryError(
                    "paired CampaignSpec adjudication requires explicit same-unit paired evidence"
                )
            interval = paired_interval(
                policy,
                paired_evidence,
                run_id=run_id,
                champion_run_id=str(champion_run["run_id"]),
                direction=str(experiment["direction"]),
                candidate_metric=candidate.metric,
                champion_metric=baseline.metric,
            )
            adjudication_evidence = interval.evidence
            # THE FLOOR IS TESTED FIRST, and it can only ever turn a PARK into an adoption.
            # An entirely-negative interval means every paired unit moved the wrong way, and
            # a gain of +0.5% or more cannot coexist with it: the interval is bootstrapped
            # from the same paired deltas whose aggregate IS this gain. The reject branch is
            # therefore left exactly where it was, BELOW this test and untouched -- do not
            # "simplify" the order by hoisting the floor past it or by folding the two into
            # one condition, because either edit turns this into a rule that adopts
            # regressions.
            #
            # A floor adoption the interval did not SUPPORT is stamped into the durable
            # evidence rather than blocked. It is adopted -- that is the decision -- and the
            # mark preserves the one fact the rule deliberately overrides, so a later audit of
            # the ratchet can separate "the evidence said yes" from "the policy said yes".
            if adopted_by_floor:
                verdict, status = "adopted", "succeeded"
                if not interval.lower > 0.0:
                    adjudication_evidence = {
                        **interval.evidence,
                        FLOOR_ADOPTION_UNSUPPORTED_BY_INTERVAL_FIELD: True,
                    }
            elif interval.lower > 0.0:
                verdict, status = "adopted", "succeeded"
            elif interval.upper < 0.0:
                verdict, status = "rejected", "succeeded"
            else:
                verdict, status = "parked", "succeeded"
        elif method == LEGACY_SCALAR_ROPE:
            if paired_evidence is not None:
                raise RegistryError(
                    "paired evidence was supplied to a legacy_scalar_rope experiment"
                )
            # `gain` above IS this branch's `improvement`, signed once by `adoption_gain`
            # instead of a second time here -- one sign convention for both branches and both
            # adjudication paths.
            rope = float(experiment["rope"])
            # The floor binds here too. Leaving this branch rope-only would leave the registry
            # answering one question two ways depending on which method a campaign declared,
            # and "0.5% is a win" is a statement about the campaign's metric, not about the
            # machinery that measured it. The reject branch keeps its place for the same
            # reason as above: a floor-sized gain is positive and cannot be a rope-width loss.
            if adopted_by_floor or gain > rope:
                verdict, status = "adopted", "succeeded"
                if adopted_by_floor and gain <= rope:
                    # The rope path's own mark: adopted by policy over a gain the measured
                    # bar could not distinguish from noise.
                    adjudication_evidence = {FLOOR_ADOPTION_INSIDE_ROPE_FIELD: True}
            elif abs(gain) <= rope:
                verdict, status = "parked", "succeeded"
            else:
                verdict, status = "rejected", "succeeded"
        else:
            raise RegistryError(
                "metric.adjudication.method must explicitly be paired_bootstrap_percentile "
                "or legacy_scalar_rope"
            )

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
                              model_version=values, adjudication_evidence=adjudication_evidence)
    else:
        adjudicate_run(
            registry, run_id=run_id, verdict=verdict, status=status, reason=reason,
            adjudication_evidence=adjudication_evidence,
        )
    if verdict == "rejected" and counterfactual_run_id is not None:
        from .registry_ratchet import consider_rejection
        consider_rejection(
            registry, run_id=run_id, model_id=model_id,
            counterfactual_run_id=counterfactual_run_id,
            intervention_digest=str(intervention_digest),
        )
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
