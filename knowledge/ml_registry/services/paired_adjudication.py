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


@dataclass(frozen=True)
class PairedInterval:
    point_estimate: float
    lower: float
    upper: float
    evidence: dict[str, object]


def comparison_policy(registry: Any, experiment_id: str) -> Mapping[str, object] | None:
    """Return the immutable CampaignSpec adjudication policy, when one was registered."""
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
    expected_policy = {
        "method", "resamples", "confidence_level", "seed", "aggregation",
    }
    if set(policy) != expected_policy:
        raise RegistryError(
            "metric.adjudication requires exactly method, resamples, confidence_level, "
            f"seed, and aggregation; missing={sorted(expected_policy - set(policy))}, "
            f"extra={sorted(set(policy) - expected_policy)}"
        )
    if policy.get("method") != PAIRED_BOOTSTRAP:
        raise RegistryError(
            f"paired evidence requires metric.adjudication.method={PAIRED_BOOTSTRAP!r}"
        )
    resamples = _integer(policy.get("resamples"), "metric.adjudication.resamples", minimum=2)
    confidence = _confidence(policy.get("confidence_level"), "metric.adjudication.confidence_level")
    seed = _integer(policy.get("seed"), "metric.adjudication.seed", minimum=0)
    aggregation = policy.get("aggregation")
    if aggregation not in {MEAN, MACRO_STRATA}:
        raise RegistryError(
            f"metric.adjudication.aggregation must be {MEAN!r} or {MACRO_STRATA!r}"
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
