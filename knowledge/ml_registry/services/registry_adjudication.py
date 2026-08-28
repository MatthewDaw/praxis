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
        if isinstance(stored_evidence, Mapping) and stored_evidence.get("method") == "vector_pareto":
            # A vector adoption's recorded reason carries the deciding-metric summary; a
            # faithful retry supplies the same base reason, so rebuild the recorded string
            # from the durable decision -- a genuinely different reason still drifts.
            reason = f"{reason} -- {stored_evidence.get('decision')}"
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
    # A VECTOR AMENDMENT IS A RE-BASELINE. If this campaign changed the dimension of its
    # judged vector after the champion was measured, the champion's number was produced by a
    # judge that no longer exists and the pair is not a comparison. Refused, naming both
    # vectors -- the same refusal, for the same reason, as runs fed differently.
    from .paired_adjudication import guard_vector_rebaseline

    guard_vector_rebaseline(
        registry, experiment_id=str(run["experiment_id"]), run_id=run_id,
        champion_run_id=str(champion_run["run_id"]),
    )
    candidate = RunMetrics.from_mapping(json.loads(run["metrics"]))
    baseline = RunMetrics.from_mapping(json.loads(champion_run["metrics"]))

    # There is deliberately NO throughput gate here. It used to read
    #     elif candidate.throughput < float(experiment["baseline_throughput"]):
    #         verdict, status = "voided", "voided"
    # and it was removed because SCORE decides a run and cost does not: an expensive arm that
    # scores better is adopted. The gate never guarded what it appeared to -- it refuses runs BELOW
    # the floor, so a degenerate FAST arm always passed it; it only ever punished slow ones. What it
    # did do was stall campaigns whose floor was unbeatable. a05_event_spotting registered 0.29828
    # against a champion measuring 0.29307, so every faithful reproduction of its own champion
    # voided and its preflight could never pass; three sibling campaigns sat at exact parity and
    # voided on contention alone. `experiments` is immutable by SQL trigger, so those floors cannot
    # be corrected in place -- which is precisely why this belongs in code and not in per-campaign
    # data. `baseline_throughput` is still stored and still reported; nothing reads it to refuse.
    # INVALID validity still voids: that is a run with no trustworthy measurement, not a slow one.
    adjudication_evidence: Mapping[str, object] | None = None
    if candidate.validity is RunValidity.INVALID:
        verdict, status = "voided", "voided"
    elif candidate.throughput_unit is not baseline.throughput_unit:
        raise RegistryError("candidate and champion throughput units are incomparable")
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
            campaign_diagnostic_metrics,
            campaign_judged_metrics,
            campaign_metric,
            comparison_policy,
            paired_interval,
        )

        # A VECTOR JUDGE, when the registered CampaignSpec declares one, decides the whole
        # verdict per-metric and Pareto (see _adjudicate_vector); the scalar path below is
        # untouched for every campaign that judges one metric.
        judged = campaign_judged_metrics(registry, str(experiment["experiment_id"]))
        if judged is not None:
            verdict, status, adjudication_evidence, reason = _adjudicate_vector(
                judged,
                run_id=run_id,
                champion_run_id=str(champion_run["run_id"]),
                candidate=candidate,
                baseline=baseline,
                paired_evidence=paired_evidence,
                reason=reason,
            )
            return _record(registry, run_id=run_id, model_id=model_id, reason=reason,
                           promotion=promotion, counterfactual_run_id=counterfactual_run_id,
                           intervention_digest=intervention_digest, verdict=verdict,
                           status=status, adjudication_evidence=adjudication_evidence)
        # A DEMOTED METRIC STAYS REPORTED. A campaign that removed a judged metric carries
        # it in its declared DIAGNOSTICS, and its runs go on reporting every measured value
        # keyed by name even though the judge is a single scalar again. Project the judged
        # name out and ignore the rest -- the same rule the vector path applies to
        # diagnostics -- but only for a campaign that actually declared diagnostics: a
        # campaign with no such declaration reporting a vector under a scalar judge is still
        # refused immediately below, unchanged.
        spec_metric = campaign_metric(registry, str(experiment["experiment_id"]))
        candidate_value, champion_value = candidate.metric, baseline.metric
        if campaign_diagnostic_metrics(registry, str(experiment["experiment_id"])) and (
            spec_metric is not None
            and (isinstance(candidate_value, Mapping) or isinstance(champion_value, Mapping))
        ):
            judged_name = str(spec_metric.get("name", "")).strip()
            candidate_value = _judged_value(candidate_value, judged_name, "candidate")
            champion_value = _judged_value(champion_value, judged_name, "champion")
            paired_evidence = _project_scalar_evidence(paired_evidence, judged_name)
        if isinstance(candidate_value, Mapping) or isinstance(champion_value, Mapping):
            raise RegistryError(
                "run metrics are a vector (values keyed by metric name), but this "
                "campaign's judge is a single scalar metric; register a vector "
                "CampaignSpec (metrics: [...]) or report one scalar metric value"
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
        gain = adoption_gain(str(experiment["direction"]), champion_value, candidate_value)
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
                candidate_metric=candidate_value,
                champion_metric=champion_value,
            )
            adjudication_evidence = interval.evidence
            group_guard = _group_non_regression(
                dict(spec_metric or {}).get("judge"), adjudication_evidence,
                metric_name=str(dict(spec_metric or {}).get("name", "metric")),
            )
            if group_guard is not None:
                adjudication_evidence = {**adjudication_evidence, **group_guard}
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
            if group_guard is not None and group_guard["group_regressions"]:
                if group_guard["group_gains"]:
                    verdict, status = "parked", "succeeded"
                    adjudication_evidence = {
                        **adjudication_evidence,
                        "isolation_required": True,
                        "park_kind": "group_isolation",
                        "decision": (
                            "parked for group isolation: paired groups moved in opposite "
                            "directions; retain the positive signal, then rerun it scoped to "
                            "one declared group without changing the others"
                        ),
                    }
                    reason = f"{reason} -- {adjudication_evidence['decision']}"
                else:
                    verdict, status = "rejected", "succeeded"
            elif adopted_by_floor:
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

    return _record(registry, run_id=run_id, model_id=model_id, reason=reason,
                   promotion=promotion, counterfactual_run_id=counterfactual_run_id,
                   intervention_digest=intervention_digest, verdict=verdict, status=status,
                   adjudication_evidence=adjudication_evidence)


def _record(
    registry: Registry,
    *,
    run_id: str,
    model_id: str,
    reason: str,
    promotion: Mapping[str, Any] | None,
    counterfactual_run_id: str | None,
    intervention_digest: str | None,
    verdict: str,
    status: str,
    adjudication_evidence: Mapping[str, object] | None,
) -> str:
    """Record a derived verdict: promote a win atomically, otherwise adjudicate the run,
    and feed a rejection with counterfactual evidence to the ratchet. The single recording
    tail both the scalar and the vector judge return through, so they cannot diverge on
    what an adoption or a rejection DOES."""
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


def _judged_value(reported: object, name: str, label: str) -> object:
    """The judged scalar's value out of a run that also reports its diagnostics."""
    if not isinstance(reported, Mapping):
        return reported
    if name not in reported:
        raise RegistryError(
            f"the {label} run is INVALID for adjudication: the judged metric {name!r} is "
            f"missing from its reported metrics {sorted(reported)}; a demoted metric stops "
            "adjudicating, it never replaces the one that still does"
        )
    return reported[name]


def _project_scalar_evidence(
    paired_evidence: Mapping[str, object] | None, name: str,
) -> Mapping[str, object] | None:
    """Project vector-shaped paired evidence onto the one metric that still adjudicates."""
    if paired_evidence is None:
        return None
    units = paired_evidence.get("units")
    if not isinstance(units, (list, tuple)) or not units:
        return paired_evidence
    first = units[0]
    if not isinstance(first, Mapping) or not isinstance(first.get("candidate"), Mapping):
        return paired_evidence
    from .paired_adjudication import project_vector_evidence

    return project_vector_evidence(paired_evidence, name)


def _group_non_regression(
    judge: object, evidence: Mapping[str, object], *, metric_name: str,
) -> dict[str, object] | None:
    """Read a declared group guard from durable paired-breakdown rows.

    ``per_sport`` is retained as the historical spelling. ``per_group`` applies to any
    declared data partition. Both scalar and vector judges consume the aggregation's
    already-computed rows, so neither path gets to re-aggregate raw units differently.
    """
    if not isinstance(judge, Mapping) or judge.get("non_regression") not in {
        "per_sport", "per_group",
    }:
        return None
    rows = evidence.get("stratum_breakdown")
    if not isinstance(rows, (list, tuple)) or not rows:
        raise RegistryError(
            f"metric {metric_name!r} per-group non-regression requires paired stratum breakdown"
        )
    deltas: dict[str, float] = {}
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise RegistryError(
                f"metric {metric_name!r} stratum_breakdown[{index}] must be an object"
            )
        group, delta = raw.get("stratum"), raw.get("delta")
        if (not isinstance(group, str) or not group.strip() or isinstance(delta, bool)
                or not isinstance(delta, (int, float))):
            raise RegistryError(
                f"metric {metric_name!r} stratum_breakdown[{index}] requires stratum and delta"
            )
        deltas[group.strip()] = float(delta)
    regressions = {group: delta for group, delta in deltas.items() if delta < 0.0}
    gains = {group: delta for group, delta in deltas.items() if delta > 0.0}
    return {
        "group_non_regression": not bool(regressions),
        "group_deltas": deltas,
        "group_gains": gains,
        "group_regressions": regressions,
    }


def _one(rows: list[dict[str, Any]], field: str, value: object, noun: str) -> dict[str, Any]:
    match = next((row for row in rows if row[field] == value), None)
    if match is None:
        raise RegistryError(f"unknown {noun}")
    return match


def _adjudicate_vector(
    entries,
    *,
    run_id: str,
    champion_run_id: str,
    candidate: RunMetrics,
    baseline: RunMetrics,
    paired_evidence: Mapping[str, object] | None,
    reason: str,
) -> tuple[str, str, dict[str, object], str]:
    """Judge a run on the campaign's declared metric VECTOR; adoption is Pareto.

    Every judged metric is compared on the SAME units with its OWN paired interval, floor
    and direction -- exactly the scalar path's tests, run per metric by the scalar helpers
    themselves. The verdict:

    - ADOPTED: at least one judged metric clears its adoption floor (or its paired lower
      bound says gain) AND the other metrics have no negative estimated effect. A win on
      one output with the rest unchanged is a win.
    - PARKED FOR ISOLATION: a run both wins and has a negative paired-interval midpoint on
      another metric. Preserve the evidence, but repeat the hypothesis with exactly one
      optimization head changed; a bundled trade must not ratchet the champion or discard
      every constituent idea.
    - REJECTED: a run has a regression and no wins. A trade never becomes an adoption merely
      because gains elsewhere are larger.
    - PARKED otherwise -- nothing cleared a floor and nothing regressed.

    A run missing any judged metric is INVALID for adjudication and is REFUSED naming the
    metric -- never adjudicated on the subset. Values a run or a unit reports beyond the
    judged names are diagnostics and are ignored. Returns
    ``(verdict, status, adjudication_evidence, reason)`` with the per-metric result (gain,
    interval, floor, outcome) durable in the evidence and the deciding metrics named, with
    their numbers, in the reason.
    """
    from knowledge.ml_registry.floor import (
        FLOOR_ADOPTION_UNSUPPORTED_BY_INTERVAL_FIELD,
        adoption_gain,
        clears_adoption_floor,
        declared_adoption_floor,
    )

    from .paired_adjudication import (
        VECTOR_PARETO,
        evidence_digest,
        paired_interval,
        project_vector_evidence,
    )

    if paired_evidence is None:
        raise RegistryError(
            "vector CampaignSpec adjudication requires explicit same-unit paired evidence"
        )
    names = [str(entry["name"]).strip() for entry in entries]
    for label, reported in (("candidate", candidate.metric), ("champion", baseline.metric)):
        if not isinstance(reported, Mapping):
            raise RegistryError(
                f"the {label} run reports one scalar metric, but this campaign judges the "
                f"vector {names}; report every judged metric's value keyed by name"
            )
        missing = sorted(set(names) - set(reported))
        if missing:
            raise RegistryError(
                f"the {label} run is INVALID for vector adjudication: judged metric(s) "
                f"{missing} missing from its reported metrics {sorted(reported)}; a vector "
                "judge never adjudicates on a subset of its metrics"
            )

    per_metric: dict[str, dict[str, object]] = {}
    regressions: list[str] = []
    negative_estimates: list[tuple[str, str]] = []
    wins: list[str] = []
    for entry in entries:
        name = str(entry["name"]).strip()
        direction = str(entry["direction"])
        candidate_value = float(candidate.metric[name])
        champion_value = float(baseline.metric[name])
        gain = adoption_gain(direction, champion_value, candidate_value)
        floor = declared_adoption_floor(dict(entry))
        adopted_by_floor = clears_adoption_floor(dict(entry), gain)
        policy = dict(entry["adjudication"])
        projected = project_vector_evidence(paired_evidence, name)
        # Each judged metric keeps its own frozen bootstrap contract. Overlay it so a
        # vector whose metrics inherited different seeds (AP50/IDF1 vs team vs possession)
        # is judged under that metric's CampaignSpec, not a single shared seed.
        for field in ("resamples", "confidence_level", "seed"):
            if field in policy:
                projected[field] = policy[field]
        interval = paired_interval(
            policy,
            projected,
            run_id=run_id,
            champion_run_id=champion_run_id,
            direction=direction,
            candidate_metric=candidate_value,
            champion_metric=champion_value,
        )
        metric_evidence = dict(interval.evidence)
        group_guard = _group_non_regression(entry.get("judge"), metric_evidence, metric_name=name)
        if group_guard is not None:
            metric_evidence.update(group_guard)
        stratum_regressions = (group_guard or {}).get("group_regressions", {})
        # Compatibility names retained for current court campaigns and their reports.
        if (group_guard is not None and isinstance(entry.get("judge"), Mapping)
                and entry["judge"].get("non_regression") == "per_sport"):
            metric_evidence["per_sport_non_regression"] = not bool(stratum_regressions)
            metric_evidence["per_sport_gains"] = group_guard["group_deltas"]
        # The same rule, in the same order, as the scalar paired path: the floor is tested
        # FIRST and can only ever turn a park into a win -- a floor-sized gain cannot
        # coexist with an entirely-negative interval bootstrapped from the same deltas.
        if adopted_by_floor:
            outcome = "floor_cleared"
            if not interval.lower > 0.0:
                metric_evidence[FLOOR_ADOPTION_UNSUPPORTED_BY_INTERVAL_FIELD] = True
        elif interval.lower > 0.0:
            outcome = "gained"
        elif interval.upper < 0.0:
            outcome = "regressed"
        else:
            outcome = "within_rope"
        summary = (f"{name} gain {gain:+.6g} (floor {floor:.6g}, "
                   f"interval [{interval.lower:.6g}, {interval.upper:.6g}])")
        if interval.lower + interval.upper < 0.0:
            negative_estimates.append((name, summary))
        group_mixed = bool(stratum_regressions and (group_guard or {}).get("group_gains"))
        if stratum_regressions:
            detail = ", ".join(
                f"{stratum} {value:+.6g}"
                for stratum, value in stratum_regressions.items()
            )
            summary += f"; per-group regression [{detail}]"
        if group_mixed:
            outcome = "mixed_groups"
            summary += "; mixed declared groups require isolation"
        elif stratum_regressions:
            outcome = "regressed"
        if outcome == "regressed":
            regressions.append(summary)
        elif outcome not in {"within_rope", "mixed_groups"}:
            wins.append(summary)
        elif outcome == "mixed_groups":
            # The pooled score may be inside its rope while one group clearly improved;
            # that is still useful causal signal and must be isolated, not discarded.
            wins.append(summary)
        per_metric[name] = {
            "direction": direction,
            "gain": gain,
            "adoption_floor": floor,
            "floor_cleared": adopted_by_floor,
            "regressed": outcome == "regressed",
            "outcome": outcome,
            **metric_evidence,
        }

    group_mixes = [name for name, item in per_metric.items()
                   if item["outcome"] == "mixed_groups"]
    if group_mixes or (wins and negative_estimates):
        verdict = "parked"
        deciding = sorted(set(group_mixes + [name for name, _ in negative_estimates]))
        kind = "group_isolation" if group_mixes else "head_isolation"
        decision = (
            f"parked for {kind.replace('_', ' ')}: mixed vector evidence; keep the measured "
            "gains, but rerun the hypothesis against this champion with exactly one "
            "optimization head or declared group branch changed. "
            + ("Negative estimated effect on " + "; ".join(summary for _, summary in negative_estimates)
               if negative_estimates else "Opposite-sign declared group deltas require isolation")
        )
    elif regressions:
        verdict = "rejected"
        deciding = [name for name, item in per_metric.items() if item["regressed"]]
        decision = ("rejected: regression beyond its rope on " + "; ".join(regressions)
                    + ", whatever the gains elsewhere")
    elif wins:
        verdict = "adopted"
        deciding = [name for name, item in per_metric.items()
                    if item["outcome"] in {"floor_cleared", "gained"}]
        decision = "adopted: " + "; ".join(wins) + "; no judged metric regressed"
    else:
        verdict = "parked"
        deciding = []
        decision = ("parked: no judged metric cleared its adoption floor or its interval, "
                    "and none regressed")
    evidence: dict[str, object] = {
        "method": VECTOR_PARETO,
        "candidate_run_id": run_id,
        "champion_run_id": champion_run_id,
        "judged_metrics": names,
        "metrics": per_metric,
        "deciding_metrics": deciding,
        "isolation_required": bool(group_mixes or (wins and negative_estimates)),
        **({"park_kind": kind} if group_mixes or (wins and negative_estimates) else {}),
        "decision": decision,
        "input_sha256": evidence_digest(paired_evidence),
    }
    return verdict, "succeeded", evidence, f"{reason} -- {decision}"
