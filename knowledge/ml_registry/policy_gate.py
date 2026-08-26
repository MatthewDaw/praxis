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


def _validate_disposition(spec: CampaignSpec) -> None:
    model_family = spec.production.get("model_family")
    for index, artifact in enumerate(spec.produces):
        artifact_type = artifact.get("artifact_type")
        if (
            model_family == "measurement_only_no_weights"
            and isinstance(artifact_type, str)
            and "checkpoint" in artifact_type.casefold()
        ):
            raise ContractError(
                "production.model_family declares 'measurement_only_no_weights', but "
                f"produces[{index}].artifact_type emits checkpoint {artifact_type!r}; remove the "
                "checkpoint artifact or declare a weights-bearing model family"
            )


def _compute_cross_corpus_rope(
    spec: CampaignSpec,
    scoring_corpora: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, object]:
    """Compute an explicitly declared global rope without erasing source provenance."""

    metric = spec.metric
    if "scoring_corpus" in metric:
        raise ContractError(
            "metric.scoring_scope 'cross_corpus' uses metric.scoring_corpora, not "
            "metric.scoring_corpus; declare every contributing corpus without relabelling it"
        )
    raw_ids = metric.get("scoring_corpora")
    if not isinstance(raw_ids, (list, tuple)) or not raw_ids:
        raise ContractError(
            "metric.scoring_scope 'cross_corpus' requires a non-empty "
            "metric.scoring_corpora sequence of declared corpus ids"
        )
    corpus_ids = tuple(
        _text(value, f"metric.scoring_corpora[{index}]")
        for index, value in enumerate(raw_ids)
    )
    if len(set(corpus_ids)) != len(corpus_ids):
        raise ContractError(
            "metric.scoring_corpora contains a duplicate corpus id; declare each provenance source once"
        )
    declarations = {
        str(corpus.get("id")): corpus
        for corpus in spec.corpora
        if isinstance(corpus.get("id"), str)
    }
    rows: list[tuple[str, Mapping[str, object]]] = []
    for corpus_id in corpus_ids:
        declaration = declarations.get(corpus_id)
        if declaration is None:
            raise ContractError(
                f"metric.scoring_corpora names {corpus_id!r}, which is absent from corpora; "
                "declare that corpus in the spec"
            )
        roles = declaration.get("roles")
        if not isinstance(roles, (list, tuple)) or "scoring" not in roles:
            raise ContractError(
                f"corpora[{corpus_id}].roles must include 'scoring' so the rope's source is explicit"
            )
        corpus_rows = scoring_corpora.get(corpus_id)
        if not isinstance(corpus_rows, (list, tuple)) or not corpus_rows or not all(
            isinstance(row, Mapping) for row in corpus_rows
        ):
            raise ContractError(
                f"scoring_corpora[{corpus_id}] must provide at least one score row; pass every "
                "declared scoring corpus at registration so its rope can be recomputed"
            )
        rows.extend((corpus_id, row) for row in corpus_rows)
    unexpected = sorted(set(scoring_corpora) - set(corpus_ids))
    if unexpected:
        raise ContractError(
            "metric.scoring_scope 'cross_corpus' received undeclared scoring_corpora "
            f"{unexpected}; declare their provenance in metric.scoring_corpora"
        )

    split_unit = _text(metric.get("split_unit"), "metric.split_unit")
    for corpus_id in corpus_ids:
        declared_unit = declarations[corpus_id].get("split_unit")
        if declared_unit != split_unit:
            raise ContractError(
                f"metric.split_unit {split_unit!r} does not match corpora[{corpus_id}].split_unit "
                f"{declared_unit!r}; make the two fields identical"
            )
    aggregation = metric.get("aggregation")
    assert isinstance(aggregation, (list, tuple))
    for index, raw_level in enumerate(aggregation):
        level = _mapping(raw_level, f"metric.aggregation[{index}]")
        name = _text(level.get("level"), f"metric.aggregation[{index}].level")
        unit = _text(level.get("unit"), f"metric.aggregation[{name}].unit")
        minimum = level.get("minimum_sample")
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
            raise ContractError(
                f"metric.aggregation[{name}].minimum_sample must be an integer at least 1"
            )
        missing = sum(row.get(unit) in (None, "") for _, row in rows)
        if missing:
            raise ContractError(
                f"metric.aggregation[{name}].unit names {unit!r}, but {missing} scoring rows lack it; "
                f"populate {unit!r} on every row"
            )
        found = len({(corpus_id, str(row[unit])) for corpus_id, row in rows})
        if found < minimum:
            raise ContractError(
                f"metric.aggregation[{name}].minimum_sample requires {minimum} unique {unit!r} "
                f"values but found {found}; add scoring units until the declared minimum is real"
            )

    metric_name = _text(metric.get("name"), "metric.name")
    by_unit: dict[tuple[str, str], list[float]] = {}
    for index, (corpus_id, row) in enumerate(rows):
        unit = row.get(split_unit)
        if unit in (None, ""):
            raise ContractError(
                f"metric.split_unit names {split_unit!r}, but scoring row {index} lacks it; "
                "populate the split unit on every scoring row"
            )
        value = _finite(row.get(metric_name), f"scoring_corpora[{corpus_id}][{index}].{metric_name}")
        by_unit.setdefault((corpus_id, str(unit)), []).append(value)
    unit_scores = tuple(statistics.fmean(values) for _, values in sorted(by_unit.items()))
    rng = random.Random(0)
    draws = tuple(
        statistics.fmean(rng.choice(unit_scores) for _ in unit_scores)
        for _ in range(BOOTSTRAP_RESAMPLES)
    )
    return {
        "method": "split_unit_bootstrap",
        "metric": metric_name,
        "scoring_scope": "cross_corpus",
        "scoring_corpora": list(corpus_ids),
        "corpus_sample_sizes": {
            corpus_id: len({unit for source, unit in by_unit if source == corpus_id})
            for corpus_id in corpus_ids
        },
        "split_unit": split_unit,
        "sample_size": len(unit_scores),
        "resamples": BOOTSTRAP_RESAMPLES,
        "value": round(statistics.stdev(draws), 12),
    }


def compute_campaign_rope(
    spec: CampaignSpec,
    scoring_corpora: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, object]:
    """Validate ``spec`` policy and return its deterministic split-unit bootstrap rope."""

    _validate_disposition(spec)
    if spec.metric.get("scoring_scope") == "cross_corpus":
        return _compute_cross_corpus_rope(spec, scoring_corpora)
    if spec.metric.get("scoring_scope", "per_corpus") != "per_corpus":
        raise ContractError("metric.scoring_scope must be 'per_corpus' or explicit 'cross_corpus'")
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
