"""Executable metric policy and scoring-corpus rope measurement for campaign registration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
import random
import statistics

from knowledge.ml_registry.contracts import CampaignSpec, ContractError


BOOTSTRAP_RESAMPLES = 2_000


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{field} must be an object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    return value.strip()


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ContractError(f"{field} must be a finite number")
    return result


def _metric_contract(spec: CampaignSpec) -> tuple[str, str, tuple[tuple[str, str, int], ...]]:
    metric = spec.metric
    operating_point = _mapping(metric.get("operating_point"), "metric.operating_point")
    selection = operating_point.get("selection")
    if selection != "frozen":
        raise ContractError(
            "metric.operating_point.selection must be 'frozen', not "
            f"{selection!r}; set it to 'frozen' and declare metric.operating_point.threshold "
            "once in the campaign spec so every arm is scored at the same operating point"
        )
    _finite(operating_point.get("threshold"), "metric.operating_point.threshold")

    raw_aggregation = metric.get("aggregation")
    if not isinstance(raw_aggregation, (list, tuple)) or not raw_aggregation:
        raise ContractError(
            "metric.aggregation must declare at least one level with level, unit, and "
            "minimum_sample so effective sample size can be checked before registration"
        )
    aggregation: list[tuple[str, str, int]] = []
    seen_levels: set[str] = set()
    for index, raw_level in enumerate(raw_aggregation):
        level = _mapping(raw_level, f"metric.aggregation[{index}]")
        name = _text(level.get("level"), f"metric.aggregation[{index}].level")
        if name in seen_levels:
            raise ContractError(f"metric.aggregation[{name}].level is duplicated; name each level once")
        seen_levels.add(name)
        unit = _text(level.get("unit"), f"metric.aggregation[{name}].unit")
        minimum = level.get("minimum_sample")
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
            raise ContractError(
                f"metric.aggregation[{name}].minimum_sample must be an integer at least 1"
            )
        aggregation.append((name, unit, minimum))
    return (
        _text(metric.get("name"), "metric.name"),
        _text(metric.get("scoring_corpus"), "metric.scoring_corpus"),
        tuple(aggregation),
    )


def _scoring_rows(
    spec: CampaignSpec,
    scoring_corpora: Mapping[str, Sequence[Mapping[str, object]]],
    corpus_id: str,
) -> tuple[Mapping[str, object], tuple[Mapping[str, object], ...]]:
    declaration = next(
        (corpus for corpus in spec.corpora if corpus.get("id") == corpus_id),
        None,
    )
    if declaration is None:
        raise ContractError(
            f"metric.scoring_corpus names {corpus_id!r}, which is absent from corpora; "
            "declare that corpus in the spec"
        )
    roles = declaration.get("roles")
    if not isinstance(roles, (list, tuple)) or "scoring" not in roles:
        raise ContractError(
            f"corpora[{corpus_id}].roles must include 'scoring' so the rope's source is explicit"
        )
    rows = scoring_corpora.get(corpus_id)
    if not isinstance(rows, (list, tuple)) or not rows or not all(
        isinstance(row, Mapping) for row in rows
    ):
        raise ContractError(
            f"scoring_corpora[{corpus_id}] must provide at least one score row; pass the named "
            "corpus at registration so its rope can be recomputed"
        )
    return declaration, tuple(rows)


def _validate_sample_sizes(
    rows: Sequence[Mapping[str, object]], aggregation: Sequence[tuple[str, str, int]],
) -> None:
    for level, unit, minimum in aggregation:
        missing = sum(row.get(unit) in (None, "") for row in rows)
        if missing:
            raise ContractError(
                f"metric.aggregation[{level}].unit names {unit!r}, but {missing} scoring rows "
                f"lack it; populate {unit!r} on every row"
            )
        found = len({str(row[unit]) for row in rows})
        if found < minimum:
            raise ContractError(
                f"metric.aggregation[{level}].minimum_sample requires {minimum} unique "
                f"{unit!r} values but found {found}; lower the declared minimum to at most "
                f"{found} only if that sample is defensible, or add scoring units until the "
                "declared minimum is real"
            )


def compute_campaign_rope(
    spec: CampaignSpec,
    scoring_corpora: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, object]:
    """Validate ``spec`` policy and return its deterministic split-unit bootstrap rope."""

    metric_name, corpus_id, aggregation = _metric_contract(spec)
    corpus, rows = _scoring_rows(spec, scoring_corpora, corpus_id)
    _validate_sample_sizes(rows, aggregation)
    split_unit = _text(spec.metric.get("split_unit"), "metric.split_unit")
    if corpus.get("split_unit") != split_unit:
        raise ContractError(
            f"metric.split_unit {split_unit!r} does not match corpora[{corpus_id}].split_unit "
            f"{corpus.get('split_unit')!r}; make the two fields identical"
        )

    by_unit: dict[str, list[float]] = {}
    for index, row in enumerate(rows):
        unit = row.get(split_unit)
        if unit in (None, ""):
            raise ContractError(
                f"metric.split_unit names {split_unit!r}, but scoring row {index} lacks it; "
                "populate the split unit on every scoring row"
            )
        value = _finite(row.get(metric_name), f"scoring_corpora[{corpus_id}][{index}].{metric_name}")
        by_unit.setdefault(str(unit), []).append(value)

    unit_scores = tuple(statistics.fmean(values) for _, values in sorted(by_unit.items()))
    rng = random.Random(0)
    draw_means = tuple(
        statistics.fmean(rng.choice(unit_scores) for _ in unit_scores)
        for _ in range(BOOTSTRAP_RESAMPLES)
    )
    rope = statistics.stdev(draw_means)
    return {
        "method": "split_unit_bootstrap",
        "metric": metric_name,
        "scoring_corpus": corpus_id,
        "split_unit": split_unit,
        "sample_size": len(unit_scores),
        "resamples": BOOTSTRAP_RESAMPLES,
        "value": round(rope, 12),
    }
