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
#: The vector judge's durable evidence method: per-metric paired bootstraps, Pareto verdict.
VECTOR_PARETO = "vector_pareto"
MEAN = "mean"
MACRO_STRATA = "macro_strata"
#: Mean of per-``sequence_split_unit`` scores. The person-model vector (and any campaign
#: whose frozen scalar is already that mean) declares this name; it is the built-in
#: ``mean`` under the split-unit vocabulary, not a second statistic.
SEQUENCE_SPLIT_UNIT = "sequence_split_unit"
#: Units carry per-class COUNTS, not a scalar. F1 is a ratio of sums, so a metric of
#: this shape is not the mean of any per-group quantity and cannot be declared `mean`
#: without adjudicating a different number than the one measured and registered.
POOLED_COUNTS = "pooled_counts_over_resampled_groups"
#: A THREE-level macro: unit scores are meaned within a (truth_kind, corpus) cell, cells are
#: meaned within a truth_kind, and truth kinds are meaned. `macro_strata` is the two-level case
#: and computes a DIFFERENT number whenever the cells of a kind hold unequal unit counts, so a
#: campaign whose frozen scalar is nested cannot declare it without adjudicating a scalar other
#: than the one it measured and registered.
MACRO_TRUTH_KIND_CORPUS_GROUP = "macro_truth_kind_corpus_group"


@dataclass(frozen=True)
class PairedInterval:
    point_estimate: float
    lower: float
    upper: float
    evidence: dict[str, object]


#: The event a campaign writes when it CHANGES THE DIMENSION of its judged vector -- a
#: metric added, or a metric demoted to a diagnostic. It carries the whole amended spec, so
#: the effective judge is read the same way whether or not a campaign ever amended.
VECTOR_AMENDED = "campaign_vector_amended"


def effective_campaign_spec(registry: Any, experiment_id: str) -> Mapping[str, object] | None:
    """The campaign spec AS IT NOW JUDGES: the last registered spec, then every later
    vector amendment folded over it in order, or ``None`` when none was ever registered.

    A campaign's judged vector is allowed to change dimension after registration (a metric
    the product turns out to consume, a metric that turns out to be a proxy for another).
    An amendment appends the whole amended spec rather than a patch, so this reader stays a
    fold over the log and every downstream judge -- scalar or vector -- keeps reading ONE
    declaration site.
    """
    spec: Mapping[str, object] | None = None
    for event in registry.list_events():
        payload = event.payload
        if payload.get("campaign_id") != experiment_id:
            continue
        if event.event_type == "campaign_spec_registered":
            spec = payload
        elif event.event_type == VECTOR_AMENDED:
            amended = payload.get("spec")
            if not isinstance(amended, Mapping):
                raise RegistryError("a judged vector amendment must carry its amended spec")
            spec = amended
    return spec


def campaign_diagnostic_metrics(
    registry: Any, experiment_id: str,
) -> tuple[Mapping[str, object], ...]:
    """Every metric this campaign MEASURES AND REPORTS but does not adjudicate on.

    A metric removed from the judged vector is DEMOTED, never deleted: it keeps being
    measured and keeps appearing in evidence, it merely stops deciding. Otherwise removing
    an objective would be indistinguishable from hiding a regression behind it.
    """
    diagnostics: tuple[Mapping[str, object], ...] = ()
    for event in registry.list_events():
        if event.event_type != VECTOR_AMENDED:
            continue
        if event.payload.get("campaign_id") != experiment_id:
            continue
        raw = event.payload.get("diagnostic_metrics") or ()
        diagnostics = tuple(dict(item) for item in raw)
    return diagnostics


def guard_vector_rebaseline(
    registry: Any, *, experiment_id: str, run_id: str, champion_run_id: str,
) -> None:
    """REFUSE to pair a run judged under an amended vector against a champion measured
    under the old one, naming both vectors -- never warn.

    Changing the vector is a re-freeze AND a re-baseline: the champion's number was
    produced by a judge that no longer exists, so the pair is not a comparison. The
    campaign re-measures its champion under the amended vector and promotes that run as its
    baseline; only then do arms resume. This is the same refusal, for the same reason, as
    "runs fed differently are not comparable".
    """
    amendments = [
        event for event in registry.list_events()
        if event.event_type == VECTOR_AMENDED
        and event.payload.get("campaign_id") == experiment_id
    ]
    if not amendments:
        return
    amendment = amendments[-1]
    promotions = [
        event.sequence for event in registry.list_events()
        if event.event_type in {"run_adopted", "run_baselined", "run_created"}
        and event.payload.get("run_id") == champion_run_id
    ]
    if promotions and max(promotions) > amendment.sequence:
        return
    old_vector = amendment.payload.get("old", {}).get("judged_metrics")
    new_vector = amendment.payload.get("new", {}).get("judged_metrics")
    raise RegistryError(
        f"champion run {champion_run_id!r} was measured under the judged vector {old_vector}, "
        f"but run {run_id!r} is judged under the amended vector {new_vector}; runs judged "
        "under different vectors are not comparable, so this pair is refused. Re-measure the "
        "champion under the amended vector and promote that run as the campaign's baseline "
        "before adjudicating arms."
    )


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
    spec = effective_campaign_spec(registry, experiment_id)
    if spec is None:
        return None
    metric = spec.get("metric")
    if not isinstance(metric, Mapping):
        raise RegistryError("registered CampaignSpec metric must be an object")
    guard_adoption_floor(metric)
    return metric


def campaign_judged_metrics(registry: Any, experiment_id: str) -> tuple[Mapping[str, object], ...] | None:
    """The VECTOR judge for this campaign -- the registered CampaignSpec's ``metrics``
    list, frozen before any run -- or ``None`` when the campaign judges a scalar (or no
    spec was ever registered). The vector twin of :func:`campaign_metric`: same event,
    same freeze, and each entry is validated by the SAME per-metric readers, so a vector
    campaign cannot hold a judging number a scalar campaign would have refused.
    """
    spec = effective_campaign_spec(registry, experiment_id)
    if spec is None:
        return None
    raw = spec.get("metrics")
    if raw is None:
        return None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw or not all(
        isinstance(item, Mapping) for item in raw
    ):
        raise RegistryError("registered CampaignSpec metrics must be a non-empty list of objects")
    entries = tuple(raw)
    guard_vector_judge(entries)
    return entries


def guard_vector_judge(entries: Sequence[Mapping[str, object]]) -> None:
    """Validate every judged metric of a vector judge, refusing by metric name.

    Each entry must carry what adjudication will read from it: a unique non-empty
    ``name``, a ``direction`` :func:`~knowledge.ml_registry.floor.adoption_gain` can sign
    a delta with, an adoption floor :func:`guard_adoption_floor` accepts, and a
    ``paired_bootstrap_percentile`` adjudication policy -- ``legacy_scalar_rope`` is
    refused because the experiment row carries ONE rope and one rope cannot bar several
    judged metrics measured on different scales.
    """
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RegistryError(f"metrics[{index}].name must be a non-empty string")
        name = name.strip()
        if name in seen:
            raise RegistryError(f"metrics names judged metric {name!r} more than once")
        seen.add(name)
        direction = entry.get("direction")
        if direction not in {"maximize", "minimize"}:
            raise RegistryError(
                f"metrics[{name}].direction must be 'maximize' or 'minimize', got {direction!r}"
            )
        try:
            guard_adoption_floor(entry)
        except RegistryError as exc:
            raise RegistryError(f"metrics[{name}]: {exc}") from exc
        policy = entry.get("adjudication")
        if not isinstance(policy, Mapping):
            raise RegistryError(
                f"metrics[{name}].adjudication must be an object declaring "
                f"method={PAIRED_BOOTSTRAP!r}"
            )
        if policy.get("method") != PAIRED_BOOTSTRAP:
            raise RegistryError(
                f"metrics[{name}].adjudication.method must be {PAIRED_BOOTSTRAP!r} under a "
                f"vector judge, got {policy.get('method')!r}; the experiment row carries one "
                "scalar rope, which cannot bar several judged metrics"
            )


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


_CHAMPION_COLUMN_TOL = 1e-6


def _require_paired_baseline(
    evidence: Mapping[str, object], *, run_id: str, champion_run_id: str,
) -> None:
    """Paired evidence must name the champion it was measured against.

    ``champion_run_id`` matching the argument is not enough: campaigns that omit
    ``baseline_run_id`` (or copy units from a different baseline) must be refused by
    name so they migrate rather than adjudicate against the wrong numbers.
    """
    baseline = evidence.get("baseline_run_id")
    if baseline != champion_run_id:
        raise RegistryError(
            f"paired evidence requires baseline_run_id equal to the champion run being "
            f"compared (candidate run {run_id!r}, champion run {champion_run_id!r}); "
            f"got {baseline!r}. Campaigns must add baseline_run_id equal to the "
            "champion run being compared"
        )


def _require_champion_column_mean(
    units: Sequence[object],
    *,
    run_id: str,
    champion_run_id: str,
    champion_metric: float,
) -> None:
    """Refuse when the unit ``champion`` column is not the champion run's recorded metric.

    The mean of the per-unit champion scores must reproduce ``champion_metric`` (the
    champion run's registered metric) within float noise. A copy of another baseline's
    units that still names the current champion is the live failure this catches.
    """
    values: list[float] = []
    for raw in units:
        if not isinstance(raw, Mapping):
            return
        champion = raw.get("champion")
        if isinstance(champion, bool) or not isinstance(champion, (int, float)):
            return
        values.append(float(champion))
    if len(values) < 2:
        return
    column_mean = statistics.fmean(values)
    if abs(column_mean - champion_metric) > _CHAMPION_COLUMN_TOL and not math.isclose(
        column_mean, champion_metric, rel_tol=1e-6, abs_tol=_CHAMPION_COLUMN_TOL,
    ):
        raise RegistryError(
            "paired evidence champion-column mean disagrees with the champion run's "
            f"recorded metric: column mean {column_mean!r} vs champion_metric "
            f"{champion_metric!r} (candidate run {run_id!r}, champion run "
            f"{champion_run_id!r})"
        )


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
    _require_paired_baseline(evidence, run_id=run_id, champion_run_id=champion_run_id)
    handler = AGGREGATIONS.get(str(aggregation))
    if handler is not None:
        return handler(
            policy, evidence, run_id=run_id, champion_run_id=champion_run_id,
            direction=direction, candidate_metric=candidate_metric,
            champion_metric=champion_metric,
        )
    if aggregation not in {MEAN, MACRO_STRATA}:
        raise RegistryError(
            f"metric.adjudication.aggregation must be one of "
            f"{sorted({MEAN, MACRO_STRATA} | set(AGGREGATIONS))}, got {aggregation!r}"
        )

    expected_evidence = {
        "candidate_run_id", "champion_run_id", "baseline_run_id", "resamples",
        "confidence_level", "seed", "units",
    }
    if set(evidence) != expected_evidence:
        raise RegistryError(
            "paired evidence requires exactly candidate_run_id, champion_run_id, "
            "baseline_run_id, resamples, confidence_level, seed, and units; "
            f"missing={sorted(expected_evidence - set(evidence))}, "
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
        # Required keys must be present; extra identity fields are allowed so a VECTOR
        # run can carry one unit list whose per-metric aggregations need different
        # labels (stratum vs truth_kind/corpus). Extra keys are ignored, never scored.
        if not isinstance(raw, Mapping) or not unit_keys <= set(raw):
            raise RegistryError(
                f"paired evidence units[{index}] requires {sorted(unit_keys)}"
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

    if aggregation == MEAN:
        _require_champion_column_mean(
            units, run_id=run_id, champion_run_id=champion_run_id,
            champion_metric=champion_metric,
        )

    candidate_point = _aggregate(parsed, value_index=2, aggregation=str(aggregation))
    champion_point = _aggregate(parsed, value_index=3, aggregation=str(aggregation))
    if not math.isclose(candidate_point, candidate_metric, rel_tol=1e-9, abs_tol=1e-12):
        raise RegistryError(
            "paired evidence candidate aggregate differs from the candidate Run metric"
            f" -- aggregate {candidate_point!r} vs Run metric {candidate_metric!r}"
            f" (difference {candidate_point - candidate_metric:+.12g})"
        )
    if not math.isclose(champion_point, champion_metric, rel_tol=1e-9, abs_tol=1e-12):
        raise RegistryError(
            "paired evidence champion aggregate differs from the champion Run metric"
            f" -- aggregate {champion_point!r} vs Run metric {champion_metric!r}"
            f" (difference {champion_point - champion_metric:+.12g})"
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
        STRATUM_BREAKDOWN: stratum_breakdown(
            [(stratum, candidate, champion) for _unit, stratum, candidate, champion in parsed],
            sign=sign,
        ),
        "point_estimate": point,
        "interval": [lower, upper],
        "input_sha256": digest,
        "units": canonical["units"],
    }
    return PairedInterval(point, lower, upper, durable)


def project_vector_evidence(evidence: Mapping[str, object], name: str) -> dict[str, object]:
    """One judged metric's same-unit slice of a vector run's paired evidence.

    Vector evidence carries ONE unit list. Units that report ``name`` on both sides are
    that metric's paired sample; units that report neither are sibling-metric rows
    (a vector whose judged metrics live on different corpora -- AP50/IDF1 on identity
    sequences, team on GSR, possession on teamtrack) and are skipped, the same skip
    registration already applies to a union ``scoring_corpora`` map. A unit that
    reports ``name`` on only one side is a pairing bug and is refused naming the
    metric and the unit. Names a unit carries beyond the projected one are diagnostics
    and are ignored.
    """
    if not isinstance(evidence, Mapping):
        raise RegistryError("vector paired evidence must be an object")
    units = evidence.get("units")
    if not isinstance(units, Sequence) or isinstance(units, (str, bytes)):
        raise RegistryError("vector paired evidence units must be a sequence of same-unit objects")
    projected_units: list[dict[str, object]] = []
    for index, raw in enumerate(units):
        if not isinstance(raw, Mapping):
            raise RegistryError(f"vector paired evidence units[{index}] must be an object")
        candidate_values = raw.get("candidate")
        champion_values = raw.get("champion")
        if not isinstance(candidate_values, Mapping) or not isinstance(champion_values, Mapping):
            raise RegistryError(
                f"vector paired evidence units[{index}] candidate and champion must be "
                "objects of values keyed by metric name"
            )
        candidate_has = name in candidate_values
        champion_has = name in champion_values
        if not candidate_has and not champion_has:
            continue
        if candidate_has != champion_has:
            side = "candidate" if candidate_has else "champion"
            raise RegistryError(
                f"vector paired evidence units[{index}].{side} has judged metric "
                f"{name!r} on one side only; both sides or neither"
            )
        unit = dict(raw)
        unit["candidate"] = candidate_values[name]
        unit["champion"] = champion_values[name]
        projected_units.append(unit)
    if len(projected_units) < 2:
        raise RegistryError(
            f"vector paired evidence has {len(projected_units)} unit(s) carrying judged "
            f"metric {name!r}; each judged metric needs at least two same-unit pairs"
        )
    projected = dict(evidence)
    projected["units"] = projected_units
    _require_paired_baseline(
        projected,
        run_id=str(projected.get("candidate_run_id", "")),
        champion_run_id=str(projected.get("champion_run_id", "")),
    )
    return projected


#: The durable, reporting-only field carrying :func:`stratum_breakdown`'s rows.
STRATUM_BREAKDOWN = "stratum_breakdown"


def stratum_breakdown(
    rows: Sequence[tuple[str, float, float]], *, sign: float,
) -> list[dict[str, object]]:
    """Per DECLARED stratum: unit count, candidate mean, champion mean, signed delta.

    Every aggregation that declares strata or domains -- ``macro_strata``'s ``stratum``, the
    nested macro's ``truth_kind:corpus`` cell, pooled counts' resampled group -- reports them
    through this ONE function, so the durable evidence has a single breakdown shape no matter
    which branch judged the run. ``mean`` declares no strata and reports its single ``all``
    row through the same call rather than a second, differently-shaped report.

    REPORTING ONLY. The verdict remains the frozen interval over the whole paired sample: no
    row here is tested against a floor, and no stratum can adopt, park or reject a run on its
    own. ``delta`` is signed by ``direction`` exactly as ``point_estimate`` is, so a positive
    number always means "the candidate moved this stratum the right way" and a reader cannot
    pick up the rows under one sign convention and the verdict under another.
    """
    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for stratum, candidate, champion in rows:
        grouped[stratum].append((candidate, champion))
    return [
        _breakdown_row(
            stratum,
            unit_count=len(values),
            candidate=statistics.fmean(candidate for candidate, _ in values),
            champion=statistics.fmean(champion for _, champion in values),
            sign=sign,
        )
        for stratum, values in sorted(grouped.items())
    ]


def _breakdown_row(
    stratum: str, *, unit_count: int, candidate: float, champion: float, sign: float,
) -> dict[str, object]:
    """One breakdown row, so every branch spells the same keys the same way."""
    return {
        "stratum": stratum,
        "unit_count": unit_count,
        "candidate_mean": candidate,
        "champion_mean": champion,
        "delta": sign * (candidate - champion),
    }


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
) -> PairedInterval:
    """The pooled-counts branch, returning what every other aggregation returns.

    It MUST be a :class:`PairedInterval`: ``registry_adjudication`` reads ``interval.evidence``,
    ``interval.lower`` and ``interval.upper`` off this value, so a bare mapping here refuses every
    run under this aggregation with an ``AttributeError`` at the adjudication seam.

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
            f" -- aggregate {candidate_point!r} vs Run metric {candidate_metric!r}"
            f" (difference {candidate_point - candidate_metric:+.12g})"
        )
    if not math.isclose(champion_point, champion_metric, rel_tol=1e-9, abs_tol=1e-12):
        raise RegistryError(
            "paired evidence champion aggregate differs from the champion Run metric"
            f" -- aggregate {champion_point!r} vs Run metric {champion_metric!r}"
            f" (difference {champion_point - champion_metric:+.12g})"
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
    # A pooled-counts unit carries per-class COUNTS, so a stratum's reported score is its own
    # pooled macro-F1 -- the very quantity the run's registered scalar means over -- rather than
    # a mean of per-unit scores, which for a ratio of sums would be a different number.
    breakdown: list[dict[str, object]] = []
    for stratum in sorted(by_stratum):
        indices = by_stratum[stratum]
        try:
            stratum_candidate = _pooled_scale_macro_f1([candidates[index] for index in indices])
            stratum_champion = _pooled_scale_macro_f1([champions[index] for index in indices])
        except RegistryError:
            # A stratum whose units carry no scored cell has no score to report. The verdict was
            # already computed from the whole sample above; a hole in the REPORT must never turn
            # an otherwise-valid adjudication into a refusal.
            continue
        breakdown.append(_breakdown_row(
            stratum, unit_count=len(indices), candidate=stratum_candidate,
            champion=stratum_champion, sign=sign,
        ))
    ordered = sorted(draws)
    tail = (1.0 - confidence) / 2.0
    lower = ordered[max(int(round(tail * (len(ordered) - 1))), 0)]
    upper = ordered[min(int(round((1.0 - tail) * (len(ordered) - 1))), len(ordered) - 1)]
    canonical = json.loads(json.dumps(dict(evidence), sort_keys=True, separators=(",", ":")))
    durable = {
        "method": PAIRED_BOOTSTRAP,
        "candidate_run_id": run_id,
        "champion_run_id": champion_run_id,
        "resamples": resamples,
        "confidence_level": confidence,
        "seed": seed,
        "aggregation": POOLED_COUNTS,
        "unit_count": len(candidates),
        "strata": sorted(by_stratum),
        STRATUM_BREAKDOWN: breakdown,
        "point_estimate": point,
        "interval": [lower, upper],
        "input_sha256": evidence_digest(evidence),
        "units": canonical["units"],
    }
    return PairedInterval(point, lower, upper, durable)


def _nested_macro_aggregate(cells: Mapping[tuple[str, str], Sequence[float]]) -> float:
    """Mean over truth kinds of the mean over that kind's corpora of the mean over its units."""
    by_kind: dict[str, list[float]] = defaultdict(list)
    for (kind, _corpus), values in cells.items():
        by_kind[kind].append(statistics.fmean(values))
    return statistics.fmean(statistics.fmean(by_kind[kind]) for kind in sorted(by_kind))


def _nested_macro_interval(
    evidence: Mapping[str, object],
    *,
    resamples: int,
    confidence: float,
    seed: int,
    run_id: str,
    champion_run_id: str,
    direction: str,
    candidate_metric: float,
    champion_metric: float,
) -> PairedInterval:
    """The ``macro_truth_kind_corpus_group`` branch of :func:`paired_interval`.

    A unit is one independent physical group and names both macro levels above it::

        {"unit_id": <group id>, "truth_kind": <outer level>, "corpus": <inner level>,
         "candidate": <scalar>, "champion": <scalar>}

    Every level is a mean, so the aggregate is linear in the unit values and the paired delta of
    the aggregates equals the aggregate of the paired deltas -- which is what is resampled, whole
    units within their (truth_kind, corpus) cell, exactly as the campaign's own frozen bootstrap
    resamples. Like every other branch it REFUSES unless the aggregate reproduces both Runs'
    registered metrics, so an aggregation named here but not measured here cannot adjudicate.
    """
    expected_evidence = {
        "candidate_run_id", "champion_run_id", "baseline_run_id", "resamples",
        "confidence_level", "seed", "units",
    }
    if set(evidence) != expected_evidence:
        raise RegistryError(
            "paired evidence requires exactly candidate_run_id, champion_run_id, "
            "baseline_run_id, resamples, confidence_level, seed, and units; "
            f"missing={sorted(expected_evidence - set(evidence))}, "
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

    unit_keys = {"unit_id", "truth_kind", "corpus", "candidate", "champion"}
    seen: set[str] = set()
    candidate_cells: dict[tuple[str, str], list[float]] = defaultdict(list)
    champion_cells: dict[tuple[str, str], list[float]] = defaultdict(list)
    delta_cells: dict[tuple[str, str], list[float]] = defaultdict(list)
    breakdown_rows: list[tuple[str, float, float]] = []
    sign = 1.0 if direction == "maximize" else -1.0
    for index, raw in enumerate(units):
        # Required keys must be present; extra identity fields are allowed so a VECTOR
        # run can carry one unit list whose per-metric aggregations need different
        # labels (stratum vs truth_kind/corpus). Extra keys are ignored, never scored.
        if not isinstance(raw, Mapping) or not unit_keys <= set(raw):
            raise RegistryError(
                f"paired evidence units[{index}] requires {sorted(unit_keys)}"
            )
        unit_id = _text(raw.get("unit_id"), f"paired evidence units[{index}].unit_id")
        if unit_id in seen:
            raise RegistryError(f"paired evidence repeats unit_id {unit_id!r}")
        seen.add(unit_id)
        cell = (
            _text(raw.get("truth_kind"), f"paired evidence units[{index}].truth_kind"),
            _text(raw.get("corpus"), f"paired evidence units[{index}].corpus"),
        )
        candidate = _finite(raw.get("candidate"), f"paired evidence units[{index}].candidate")
        champion = _finite(raw.get("champion"), f"paired evidence units[{index}].champion")
        candidate_cells[cell].append(candidate)
        champion_cells[cell].append(champion)
        delta_cells[cell].append(sign * (candidate - champion))
        # The breakdown's domain is the cell label the durable `strata` list already uses, so
        # the two fields name the same domains rather than two spellings of them.
        breakdown_rows.append((f"{cell[0]}:{cell[1]}", candidate, champion))

    if any(len(values) < 2 for values in delta_cells.values()):
        raise RegistryError(
            "paired nested-macro evidence requires at least two units per truth_kind:corpus cell"
        )
    candidate_point = _nested_macro_aggregate(candidate_cells)
    champion_point = _nested_macro_aggregate(champion_cells)
    if not math.isclose(candidate_point, candidate_metric, rel_tol=1e-9, abs_tol=1e-12):
        raise RegistryError(
            "paired evidence candidate aggregate differs from the candidate Run metric"
            f" -- aggregate {candidate_point!r} vs Run metric {candidate_metric!r}"
            f" (difference {candidate_point - candidate_metric:+.12g})"
        )
    if not math.isclose(champion_point, champion_metric, rel_tol=1e-9, abs_tol=1e-12):
        raise RegistryError(
            "paired evidence champion aggregate differs from the champion Run metric"
            f" -- aggregate {champion_point!r} vs Run metric {champion_metric!r}"
            f" (difference {champion_point - champion_metric:+.12g})"
        )

    point = _nested_macro_aggregate(delta_cells)
    rng = random.Random(seed)
    ordered_cells = sorted(delta_cells)
    draws = sorted(
        _nested_macro_aggregate({
            cell: [rng.choice(delta_cells[cell]) for _ in delta_cells[cell]]
            for cell in ordered_cells
        })
        for _ in range(resamples)
    )
    alpha = (1.0 - confidence) / 2.0
    lower, upper = _percentile(draws, alpha), _percentile(draws, 1.0 - alpha)
    canonical = json.loads(json.dumps(dict(evidence), sort_keys=True, separators=(",", ":")))
    durable = {
        "method": PAIRED_BOOTSTRAP,
        "candidate_run_id": run_id,
        "champion_run_id": champion_run_id,
        "resamples": resamples,
        "confidence_level": confidence,
        "seed": seed,
        "aggregation": MACRO_TRUTH_KIND_CORPUS_GROUP,
        "unit_count": len(seen),
        "strata": [f"{kind}:{corpus}" for kind, corpus in ordered_cells],
        STRATUM_BREAKDOWN: stratum_breakdown(breakdown_rows, sign=sign),
        "point_estimate": point,
        "interval": [lower, upper],
        "input_sha256": evidence_digest(evidence),
        "units": canonical["units"],
    }
    return PairedInterval(point, lower, upper, durable)


#: Aggregations beyond the two built-ins, keyed by the name a CampaignSpec declares.
#:
#: A metric that is not the mean of a per-unit scalar -- F1 and AP are ratios of sums, and a
#: nested macro averages within groups before across them -- cannot be expressed as ``mean`` or
#: ``macro_strata`` without adjudicating a DIFFERENT number than the Run registered. Two campaigns
#: were hard-blocked on exactly that today, each waiting on a branch being hand-written into the
#: middle of ``paired_interval``. Registering a handler is now the whole change.
#:
#: A handler takes ``(policy, evidence, *, run_id, champion_run_id, direction, candidate_metric,
#: champion_metric)`` and returns a ``PairedInterval``. It is NOT free to decide what is true: like
#: the built-ins, it must refuse unless its aggregate reproduces BOTH Runs' registered metrics.
#: That guard is the whole reason the seam is safe to open -- an aggregation that could report any
#: number it liked would be a campaign grading its own homework.
def _nested_macro_handler(
    policy: Mapping[str, object],
    evidence: Mapping[str, object],
    *,
    run_id: str,
    champion_run_id: str,
    direction: str,
    candidate_metric: float,
    champion_metric: float,
) -> PairedInterval:
    """Adapt ``_nested_macro_interval`` to the common handler signature.

    It reads resamples/confidence/seed as explicit arguments rather than off the policy, so the
    seam normalises here instead of rewriting a function two campaigns already depend on.
    """
    return _nested_macro_interval(
        evidence,
        resamples=_integer(policy.get("resamples"), "metric.adjudication.resamples", minimum=2),
        confidence=_confidence(
            policy.get("confidence_level"), "metric.adjudication.confidence_level"
        ),
        seed=_integer(policy.get("seed"), "metric.adjudication.seed", minimum=0),
        run_id=run_id,
        champion_run_id=champion_run_id,
        direction=direction,
        candidate_metric=candidate_metric,
        champion_metric=champion_metric,
    )


def _mean_alias_handler(
    policy: Mapping[str, object],
    evidence: Mapping[str, object],
    *,
    run_id: str,
    champion_run_id: str,
    direction: str,
    candidate_metric: float,
    champion_metric: float,
) -> PairedInterval:
    """``sequence_split_unit`` is the mean of per-split-unit scores -- rewrite and reuse."""
    aliased = dict(policy)
    aliased["aggregation"] = MEAN
    return paired_interval(
        aliased, evidence, run_id=run_id, champion_run_id=champion_run_id,
        direction=direction, candidate_metric=candidate_metric,
        champion_metric=champion_metric,
    )


AGGREGATIONS: dict[str, Any] = {
    POOLED_COUNTS: _pooled_counts_interval,
    MACRO_TRUTH_KIND_CORPUS_GROUP: _nested_macro_handler,
    SEQUENCE_SPLIT_UNIT: _mean_alias_handler,
}


def register_aggregation(name: str, handler: Any) -> None:
    """Register a paired aggregation under the name a CampaignSpec declares.

    Refuses to shadow a built-in or to redefine an existing name with a different handler:
    two campaigns silently disagreeing about what one aggregation name means is worse than
    either of them failing loudly.
    """
    if name in {MEAN, MACRO_STRATA}:
        raise RegistryError(f"aggregation {name!r} is built in and cannot be redefined")
    existing = AGGREGATIONS.get(name)
    if existing is not None and existing is not handler:
        raise RegistryError(f"aggregation {name!r} is already registered to a different handler")
    AGGREGATIONS[name] = handler
