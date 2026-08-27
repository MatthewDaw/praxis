"""Frozen, same-unit paired bootstrap evidence for canonical run adjudication."""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from knowledge.ml_registry.storage.registry import RegistryError


PAIRED_BOOTSTRAP = "paired_bootstrap_percentile"
LEGACY_SCALAR_ROPE = "legacy_scalar_rope"
MEAN = "mean"
MACRO_STRATA = "macro_strata"
#: Units carry per-class COUNTS, not a scalar. F1 is a ratio of sums, so a metric of
#: this shape is not the mean of any per-group quantity and cannot be declared `mean`
#: without adjudicating a different number than the one measured and registered.
POOLED_COUNTS = "pooled_counts_over_resampled_groups"


@dataclass(frozen=True)
class PairedInterval:
    point_estimate: float
    lower: float
    upper: float
    evidence: dict[str, object]


def campaign_metric(registry: Any, experiment_id: str) -> Mapping[str, object] | None:
    """THE JUDGE for this campaign -- the registered CampaignSpec's ``metric`` object, frozen
    before any run -- or ``None`` when no spec was ever registered for this experiment.

    This is the single declaration site the canonical-registry path reads its judging numbers
    from, the adoption floor included. The floor is VALIDATED here as well as at
    :meth:`Registry.register_campaign_spec`, for the same reason ``sigmas`` is validated at
    registration: a value that cannot state a gain must be refused before it silently becomes
    the default at adjudication time. It is read by
    :func:`~knowledge.ml_registry.floor.declared_adoption_floor` -- the SAME reader, default
    and validation the Praxis-space path uses -- so the two paths cannot hold two numbers.
    """
    specs = [
        event.payload
        for event in registry.list_events()
        if event.event_type == "campaign_spec_registered"
        and event.payload.get("campaign_id") == experiment_id
    ]
    if not specs:
        return None
    metric = specs[-1].get("metric")
    if not isinstance(metric, Mapping):
        raise RegistryError("registered CampaignSpec metric must be an object")
    guard_adoption_floor(metric)
    return metric


def guard_adoption_floor(metric: Mapping[str, object]) -> float:
    """Read (and so validate) the declared adoption floor, in this registry's error dialect.

    The canonical registry speaks :class:`RegistryError`; the floor's reader raises the
    Praxis-space :class:`RegistryValidationError`. Converting HERE, once, is what lets both
    paths share one reader without either one inventing its own threshold or its own default.
    """
    from knowledge.ml_registry.floor import declared_adoption_floor
    from knowledge.ml_registry.schema import RegistryValidationError

    try:
        return declared_adoption_floor(dict(metric))
    except RegistryValidationError as exc:
        raise RegistryError(str(exc)) from exc


def comparison_policy(registry: Any, experiment_id: str) -> Mapping[str, object] | None:
    """Return the immutable CampaignSpec adjudication policy, when one was registered."""
    metric = campaign_metric(registry, experiment_id)
    if metric is None:
        return None
    policy = metric.get("adjudication")
    if not isinstance(policy, Mapping):
        raise RegistryError(
            "registered CampaignSpec must explicitly declare metric.adjudication as "
            f"{PAIRED_BOOTSTRAP!r} or {LEGACY_SCALAR_ROPE!r}"
        )
    return policy


def evidence_digest(evidence: Mapping[str, object]) -> str:
    """Hash the exact paired input using its canonical JSON representation."""
    try:
        encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RegistryError("paired evidence must be finite JSON data") from exc
    return hashlib.sha256(encoded.encode()).hexdigest()


def paired_interval(
    policy: Mapping[str, object],
    evidence: Mapping[str, object],
    *,
    run_id: str,
    champion_run_id: str,
    direction: str,
    candidate_metric: float,
    champion_metric: float,
) -> PairedInterval:
    """Validate evidence against the frozen policy and bootstrap its paired delta."""
    required_policy = {
        "method", "resamples", "confidence_level", "seed", "aggregation",
    }
    permitted_policy = required_policy | {"effect_floor", "law"}
    if not required_policy <= set(policy) or not set(policy) <= permitted_policy:
        raise RegistryError(
            "metric.adjudication requires method, resamples, confidence_level, seed, and "
            "aggregation; optional effect_floor/law are accepted only for the frozen no-floor "
            f"paired law; missing={sorted(required_policy - set(policy))}, "
            f"extra={sorted(set(policy) - permitted_policy)}"
        )
    if policy.get("method") != PAIRED_BOOTSTRAP:
        raise RegistryError(
            f"paired evidence requires metric.adjudication.method={PAIRED_BOOTSTRAP!r}"
        )
    if "effect_floor" in policy and _finite(
        policy["effect_floor"], "metric.adjudication.effect_floor"
    ) != 0.0:
        raise RegistryError("paired interval adjudication permits only effect_floor=0.0")
    if "law" in policy and policy["law"] != (
        "95% paired interval entirely positive ADOPT, crosses zero PARK, "
        "entirely negative REJECT"
    ):
        raise RegistryError("paired interval adjudication law must preserve the zero-threshold CI rule")
    resamples = _integer(policy.get("resamples"), "metric.adjudication.resamples", minimum=2)
    confidence = _confidence(policy.get("confidence_level"), "metric.adjudication.confidence_level")
    seed = _integer(policy.get("seed"), "metric.adjudication.seed", minimum=0)
    aggregation = policy.get("aggregation")
    if aggregation == "stitch_decision":
        # A stitch decision is one independent paired unit; its frozen aggregation is the mean.
        aggregation = MEAN
    if aggregation == POOLED_COUNTS:
        return _pooled_counts_interval(
            policy, evidence, run_id=run_id, champion_run_id=champion_run_id,
            direction=direction, candidate_metric=candidate_metric,
            champion_metric=champion_metric,
        )
    if aggregation not in {MEAN, MACRO_STRATA}:
        raise RegistryError(
            f"metric.adjudication.aggregation must be {MEAN!r}, {MACRO_STRATA!r} "
            f"or {POOLED_COUNTS!r}"
        )

    expected_evidence = {
        "candidate_run_id", "champion_run_id", "resamples", "confidence_level", "seed", "units",
    }
    if set(evidence) != expected_evidence:
        raise RegistryError(
            "paired evidence requires exactly candidate_run_id, champion_run_id, resamples, "
            f"confidence_level, seed, and units; missing={sorted(expected_evidence - set(evidence))}, "
            f"extra={sorted(set(evidence) - expected_evidence)}"
        )
    if evidence.get("candidate_run_id") != run_id:
        raise RegistryError("paired evidence candidate_run_id does not name the adjudicated run")
    if evidence.get("champion_run_id") != champion_run_id:
        raise RegistryError("paired evidence champion_run_id does not name the current champion run")
    if evidence.get("resamples") != resamples:
        raise RegistryError("paired evidence resamples differs from the frozen CampaignSpec")
    if evidence.get("confidence_level") != confidence:
        raise RegistryError("paired evidence confidence_level differs from the frozen CampaignSpec")
    if evidence.get("seed") != seed:
        raise RegistryError("paired evidence seed differs from the frozen CampaignSpec")
    units = evidence.get("units")
    if not isinstance(units, Sequence) or isinstance(units, (str, bytes)) or len(units) < 2:
        raise RegistryError("paired evidence units must contain at least two same-unit comparisons")

    parsed: list[tuple[str, str, float, float]] = []
    seen: set[str] = set()
    unit_keys = {"unit_id", "candidate", "champion"}
    if aggregation == MACRO_STRATA:
        unit_keys.add("stratum")
    for index, raw in enumerate(units):
        if not isinstance(raw, Mapping) or set(raw) != unit_keys:
            raise RegistryError(
                f"paired evidence units[{index}] requires exactly {sorted(unit_keys)}"
            )
        unit_id = _text(raw.get("unit_id"), f"paired evidence units[{index}].unit_id")
        if unit_id in seen:
            raise RegistryError(f"paired evidence repeats unit_id {unit_id!r}")
        seen.add(unit_id)
        stratum = (
            _text(raw.get("stratum"), f"paired evidence units[{index}].stratum")
            if aggregation == MACRO_STRATA else "all"
        )
        parsed.append((
            unit_id,
            stratum,
            _finite(raw.get("candidate"), f"paired evidence units[{index}].candidate"),
            _finite(raw.get("champion"), f"paired evidence units[{index}].champion"),
        ))

    candidate_point = _aggregate(parsed, value_index=2, aggregation=str(aggregation))
    champion_point = _aggregate(parsed, value_index=3, aggregation=str(aggregation))
    if not math.isclose(candidate_point, candidate_metric, rel_tol=1e-9, abs_tol=1e-12):
        raise RegistryError(
            "paired evidence candidate aggregate differs from the candidate Run metric"
        )
    if not math.isclose(champion_point, champion_metric, rel_tol=1e-9, abs_tol=1e-12):
        raise RegistryError(
            "paired evidence champion aggregate differs from the champion Run metric"
        )

    sign = 1.0 if direction == "maximize" else -1.0
    by_stratum: dict[str, list[float]] = defaultdict(list)
    for _unit_id, stratum, candidate, champion in parsed:
        by_stratum[stratum].append(sign * (candidate - champion))
    if aggregation == MACRO_STRATA and any(len(values) < 2 for values in by_stratum.values()):
        raise RegistryError("paired macro-strata evidence requires at least two units per stratum")
    point = statistics.fmean(statistics.fmean(values) for values in by_stratum.values())
    rng = random.Random(seed)
    draws = sorted(
        statistics.fmean(
            statistics.fmean(rng.choice(values) for _ in values)
            for values in by_stratum.values()
        )
        for _ in range(resamples)
    )
    alpha = (1.0 - confidence) / 2.0
    lower, upper = _percentile(draws, alpha), _percentile(draws, 1.0 - alpha)
    canonical = json.loads(json.dumps(dict(evidence), sort_keys=True, separators=(",", ":")))
    digest = evidence_digest(evidence)
    durable = {
        "method": PAIRED_BOOTSTRAP,
        "candidate_run_id": run_id,
        "champion_run_id": champion_run_id,
        "resamples": resamples,
        "confidence_level": confidence,
        "seed": seed,
        "aggregation": aggregation,
        "unit_count": len(parsed),
        "strata": sorted(by_stratum),
        "point_estimate": point,
        "interval": [lower, upper],
        "input_sha256": digest,
        "units": canonical["units"],
    }
    return PairedInterval(point, lower, upper, durable)


def _aggregate(
    rows: Sequence[tuple[str, str, float, float]], *, value_index: int, aggregation: str,
) -> float:
    by_stratum: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_stratum[row[1]].append(row[value_index])
    if aggregation == MACRO_STRATA:
        return statistics.fmean(statistics.fmean(values) for values in by_stratum.values())
    return statistics.fmean(value for values in by_stratum.values() for value in values)


def _percentile(sorted_values: Sequence[float], quantile: float) -> float:
    position = quantile * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RegistryError(f"{field} must be a finite number")
    return float(value)


def _integer(value: object, field: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RegistryError(f"{field} must be an integer at least {minimum}")
    return value


def _confidence(value: object, field: str) -> float:
    result = _finite(value, field)
    if not 0.0 < result < 1.0:
        raise RegistryError(f"{field} must be strictly between zero and one")
    return result


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{field} must be a non-empty string")
    return value.strip()


def _pooled_counts_payload(raw: object, field: str) -> dict[str, dict[str, tuple[int, int, int]]]:
    if not isinstance(raw, Mapping) or not raw:
        raise RegistryError(f"{field} must be a non-empty scale-stratum mapping")
    parsed: dict[str, dict[str, tuple[int, int, int]]] = {}
    for stratum, classes in raw.items():
        if not isinstance(classes, Mapping) or not classes:
            raise RegistryError(f"{field}[{stratum!r}] must be a non-empty class mapping")
        bucket: dict[str, tuple[int, int, int]] = {}
        for class_key, cell in classes.items():
            if not isinstance(cell, Sequence) or isinstance(cell, (str, bytes)) or len(cell) != 3:
                raise RegistryError(
                    f"{field}[{stratum!r}][{class_key!r}] must be [tp, fp, fn]"
                )
            values = []
            for value in cell:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise RegistryError(
                        f"{field}[{stratum!r}][{class_key!r}] counts must be non-negative integers"
                    )
                values.append(int(value))
            bucket[str(class_key)] = (values[0], values[1], values[2])
        parsed[str(stratum)] = bucket
    return parsed


def _pooled_scale_macro_f1(
    units: Sequence[Mapping[str, dict[str, tuple[int, int, int]]]],
) -> float:
    """Pool counts over units, then macro over classes within a scale stratum, then over strata.

    A (stratum, class) with no scored cell at all contributes nothing and is not counted, so a
    resample that never presents a class is not silently credited with a zero for it.
    """
    pooled: dict[str, dict[str, list[int]]] = {}
    for unit in units:
        for stratum, classes in unit.items():
            bucket = pooled.setdefault(stratum, {})
            for class_key, (tp, fp, fn) in classes.items():
                cell = bucket.setdefault(class_key, [0, 0, 0])
                cell[0] += tp
                cell[1] += fp
                cell[2] += fn
    per_stratum: list[float] = []
    for stratum in sorted(pooled):
        members = []
        for _class_key, (tp, fp, fn) in sorted(pooled[stratum].items()):
            if tp + fp + fn == 0:
                continue
            members.append(0.0 if tp == 0 else 2.0 * tp / (2.0 * tp + fp + fn))
        if members:
            per_stratum.append(sum(members) / len(members))
    if not per_stratum:
        raise RegistryError("no scale stratum carried a scored cell")
    return sum(per_stratum) / len(per_stratum)


def _pooled_counts_interval(
    policy: Mapping[str, object],
    evidence: Mapping[str, object],
    *,
    run_id: str,
    champion_run_id: str,
    direction: str,
    candidate_metric: float,
    champion_metric: float,
) -> dict[str, object]:
    """The proposed branch, in the shape ``paired_interval`` would call it.

    Everything ``paired_interval`` already validates -- method, run ids, resamples, confidence,
    seed, unit-id uniqueness, minimum two units -- is validated the same way; only the unit VALUE
    model and the aggregate differ.
    """
    if policy.get("aggregation") != POOLED_COUNTS:
        raise RegistryError(f"this branch handles only aggregation={POOLED_COUNTS!r}")
    resamples = int(policy["resamples"])
    confidence = float(policy["confidence_level"])
    seed = int(policy["seed"])
    if evidence.get("candidate_run_id") != run_id:
        raise RegistryError("paired evidence candidate_run_id does not name the run")
    if evidence.get("champion_run_id") != champion_run_id:
        raise RegistryError("paired evidence champion_run_id does not name the champion")
    for field, expected in (("resamples", resamples), ("confidence_level", confidence),
                            ("seed", seed)):
        if evidence.get(field) != expected:
            raise RegistryError(f"paired evidence {field} differs from the frozen spec")
    raw_units = evidence.get("units")
    if not isinstance(raw_units, Sequence) or isinstance(raw_units, (str, bytes)) or len(raw_units) < 2:
        raise RegistryError("paired evidence units must contain at least two comparisons")

    seen: set[str] = set()
    by_stratum: dict[str, list[int]] = {}
    candidates: list[dict[str, dict[str, tuple[int, int, int]]]] = []
    champions: list[dict[str, dict[str, tuple[int, int, int]]]] = []
    for index, raw in enumerate(raw_units):
        if not isinstance(raw, Mapping) or set(raw) != {"unit_id", "stratum", "candidate", "champion"}:
            raise RegistryError(
                f"paired evidence units[{index}] requires exactly "
                "['candidate', 'champion', 'stratum', 'unit_id']"
            )
        unit_id = str(raw["unit_id"]).strip()
        if not unit_id:
            raise RegistryError(f"paired evidence units[{index}].unit_id must be non-empty")
        if unit_id in seen:
            raise RegistryError(f"paired evidence repeats unit_id {unit_id!r}")
        seen.add(unit_id)
        by_stratum.setdefault(str(raw["stratum"]), []).append(index)
        candidates.append(_pooled_counts_payload(raw["candidate"], f"units[{index}].candidate"))
        champions.append(_pooled_counts_payload(raw["champion"], f"units[{index}].champion"))

    candidate_point = _pooled_scale_macro_f1(candidates)
    champion_point = _pooled_scale_macro_f1(champions)
    if not math.isclose(candidate_point, candidate_metric, rel_tol=1e-9, abs_tol=1e-12):
        raise RegistryError(
            "paired evidence candidate aggregate differs from the candidate Run metric"
        )
    if not math.isclose(champion_point, champion_metric, rel_tol=1e-9, abs_tol=1e-12):
        raise RegistryError(
            "paired evidence champion aggregate differs from the champion Run metric"
        )

    sign = 1.0 if direction == "maximize" else -1.0
    point = sign * (candidate_point - champion_point)
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(resamples):
        drawn: list[int] = []
        for stratum in sorted(by_stratum):
            pool = by_stratum[stratum]
            drawn.extend(rng.choice(pool) for _ in range(len(pool)))
        try:
            draws.append(sign * (
                _pooled_scale_macro_f1([candidates[i] for i in drawn])
                - _pooled_scale_macro_f1([champions[i] for i in drawn])
            ))
        except RegistryError:
            continue
    if not draws:
        raise RegistryError("no paired bootstrap draw produced a defined delta")
    ordered = sorted(draws)
    tail = (1.0 - confidence) / 2.0
    lower = ordered[max(int(round(tail * (len(ordered) - 1))), 0)]
    upper = ordered[min(int(round((1.0 - tail) * (len(ordered) - 1))), len(ordered) - 1)]
    return {
        "method": "paired_bootstrap_percentile",
        "aggregation": POOLED_COUNTS,
        "candidate_run_id": run_id,
        "champion_run_id": champion_run_id,
        "resamples": resamples,
        "confidence_level": confidence,
        "seed": seed,
        "unit_count": len(candidates),
        "strata": sorted(by_stratum),
        "point_estimate": point,
        "interval": [lower, upper],
    }
