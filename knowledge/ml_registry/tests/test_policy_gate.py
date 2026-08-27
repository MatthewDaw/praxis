"""Executable R0a metric-contract and scoring-corpus rope acceptance."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from knowledge.ml_registry.contracts._validation import ContractError
from knowledge.ml_registry.storage import Registry


FIXTURES = Path(__file__).parent / "fixtures" / "policy_gate"


def _fixture() -> tuple[dict[str, object], dict[str, list[dict[str, object]]]]:
    spec = json.loads((FIXTURES / "campaign.json").read_text())
    rows = [json.loads(line) for line in (FIXTURES / "scoring.jsonl").read_text().splitlines()]
    return spec, {"fixture_scoring": rows}


def test_per_arm_operating_point_is_refused_with_the_field_and_fix(tmp_path: Path) -> None:
    spec, corpora = _fixture()
    bad = deepcopy(spec)
    metric = bad["metric"]
    assert isinstance(metric, dict)
    operating_point = metric["operating_point"]
    assert isinstance(operating_point, dict)
    operating_point["selection"] = "per_arm"

    with pytest.raises(ContractError) as exc_info:
        Registry(tmp_path).register_campaign_spec(bad, scoring_corpora=corpora)

    message = str(exc_info.value)
    assert "metric.operating_point.selection" in message
    assert "frozen" in message
    assert "threshold" in message


def test_every_aggregation_level_enforces_its_declared_minimum(tmp_path: Path) -> None:
    spec, corpora = _fixture()
    bad = deepcopy(spec)
    metric = bad["metric"]
    assert isinstance(metric, dict)
    aggregation = metric["aggregation"]
    assert isinstance(aggregation, list)
    level = aggregation[1]
    assert isinstance(level, dict)
    level["minimum_sample"] = 4

    with pytest.raises(ContractError) as exc_info:
        Registry(tmp_path).register_campaign_spec(bad, scoring_corpora=corpora)

    message = str(exc_info.value)
    assert "metric.aggregation[match].minimum_sample" in message
    assert "found 3" in message
    assert "at most 3" in message


def test_registration_persists_a_rope_from_the_named_scoring_corpus(tmp_path: Path) -> None:
    spec, corpora = _fixture()
    registry = Registry(tmp_path)

    assert registry.register_campaign_spec(spec, scoring_corpora=corpora)
    event = registry.list_events()[-1]
    rope = event.payload["rope"]

    assert event.event_type == "campaign_spec_registered"
    assert rope["method"] == "split_unit_bootstrap"
    assert rope["scoring_corpus"] == "fixture_scoring"
    assert rope["split_unit"] == "match_id"
    assert rope["sample_size"] == 3
    assert rope["value"] > 0
    assert not registry.register_campaign_spec(spec, scoring_corpora=corpora)


def test_each_campaigns_own_scores_determine_its_rope(tmp_path: Path) -> None:
    spec, corpora = _fixture()
    registry = Registry(tmp_path)
    assert registry.register_campaign_spec(spec, scoring_corpora=corpora)
    first_rope = registry.list_events()[-1].payload["rope"]["value"]

    second = deepcopy(spec)
    second["campaign_id"] = "fixture_policy_campaign_2"
    concentrated = deepcopy(corpora)
    for row in concentrated["fixture_scoring"]:
        row["fixture_score"] = 0.8
    assert registry.register_campaign_spec(second, scoring_corpora=concentrated)

    second_rope = registry.list_events()[-1].payload["rope"]["value"]
    assert second_rope == 0
    assert second_rope != first_rope


def test_measurement_only_campaign_cannot_emit_a_checkpoint(tmp_path: Path) -> None:
    spec, corpora = _fixture()
    spec["production"] = {
        "protocol": "FixtureProtocol",
        "model_family": "measurement_only_no_weights",
    }
    spec["produces"] = [{"artifact_type": "fixture_checkpoint"}]

    with pytest.raises(ContractError) as exc_info:
        Registry(tmp_path).register_campaign_spec(spec, scoring_corpora=corpora)

    message = str(exc_info.value)
    assert "production.model_family" in message
    assert "measurement_only_no_weights" in message
    assert "produces[0].artifact_type" in message
    assert "remove the checkpoint" in message
    assert "weights-bearing model family" in message


def test_structural_validator_refuses_an_unloadable_corpus_verbatim(tmp_path: Path) -> None:
    spec, corpora = _fixture()
    spec["corpora"] = [*spec["corpora"], {"id": "missing_training", "roles": ["training"]}]

    def fixture_t7(candidate: object) -> None:
        assert isinstance(candidate, dict)
        declarations = candidate["corpora"]
        assert isinstance(declarations, list)
        if any(item.get("id") == "missing_training" for item in declarations):
            raise ContractError(
                "corpus 'missing_training' cannot load; register a loadable DataSource for that id"
            )

    with pytest.raises(ContractError) as exc_info:
        Registry(tmp_path).register_campaign_spec(
            spec,
            scoring_corpora=corpora,
            structural_validator=fixture_t7,
        )

    assert str(exc_info.value) == (
        "corpus 'missing_training' cannot load; register a loadable DataSource for that id"
    )


def test_fixture_structural_refusal_is_surfaced_without_restating_it(tmp_path: Path) -> None:
    spec, corpora = _fixture()
    calls: list[object] = []

    def fixture_t7(candidate: object) -> None:
        calls.append(candidate)
        raise ContractError("fixture T7 says campaign_id does not match folder; rename the folder")

    with pytest.raises(ContractError) as exc_info:
        Registry(tmp_path).register_campaign_spec(
            spec,
            scoring_corpora=corpora,
            structural_validator=fixture_t7,
        )

    assert calls == [spec]
    assert str(exc_info.value) == (
        "fixture T7 says campaign_id does not match folder; rename the folder"
    )
def test_explicit_cross_corpus_registration_counts_global_units_and_keeps_provenance(
    tmp_path: Path,
) -> None:
    spec, _ = _fixture()
    metric = spec["metric"]
    assert isinstance(metric, dict)
    metric.pop("scoring_corpus")
    metric["scoring_scope"] = "cross_corpus"
    corpus_ids = [f"source_{index}" for index in range(10)]
    metric["scoring_corpora"] = corpus_ids
    aggregation = metric["aggregation"]
    assert isinstance(aggregation, list)
    aggregation[1]["minimum_sample"] = 12
    spec["corpora"] = [
        {"id": corpus_id, "roles": ["scoring"], "split_unit": "match_id"}
        for corpus_id in corpus_ids
    ]
    corpora = {
        corpus_id: [
            {"example_id": f"{corpus_id}-a", "match_id": "local-1", "fixture_score": 0.70},
            {"example_id": f"{corpus_id}-b", "match_id": "local-2", "fixture_score": 0.80},
        ]
        for corpus_id in corpus_ids
    }

    registry = Registry(tmp_path)
    assert registry.register_campaign_spec(spec, scoring_corpora=corpora)

    rope = registry.list_events()[-1].payload["rope"]
    assert rope["scoring_scope"] == "cross_corpus"
    assert rope["scoring_corpora"] == corpus_ids
    assert rope["corpus_sample_sizes"] == {corpus_id: 2 for corpus_id in corpus_ids}
    assert rope["sample_size"] == 20
    assert "scoring_corpus" not in rope


def test_cross_corpus_refuses_false_single_corpus_relabelling(tmp_path: Path) -> None:
    spec, corpora = _fixture()
    metric = spec["metric"]
    assert isinstance(metric, dict)
    metric["scoring_scope"] = "cross_corpus"
    metric["scoring_corpora"] = ["fixture_scoring"]

    with pytest.raises(ContractError) as exc_info:
        Registry(tmp_path).register_campaign_spec(spec, scoring_corpora=corpora)

    assert "uses metric.scoring_corpora, not metric.scoring_corpus" in str(exc_info.value)


def test_vector_judge_accepts_a_union_scoring_corpora_map(tmp_path: Path) -> None:
    """Per-metric scoring_corpora lists share one map; extra keys are the other metrics."""
    spec, _ = _fixture()
    spec.pop("metric")
    adjudicate = {
        "method": "paired_bootstrap_percentile",
        "resamples": 200,
        "confidence_level": 0.95,
        "seed": 1,
        "aggregation": "mean",
    }
    operating = {"selection": "frozen", "threshold": 0.5}
    aggregation = [{"level": "match", "unit": "match_id", "minimum_sample": 2}]
    spec["metrics"] = [
        {
            "name": "ap50",
            "direction": "maximize",
            "adoption_floor": 0.005,
            "operating_point": operating,
            "aggregation": aggregation,
            "scoring_scope": "cross_corpus",
            "scoring_corpora": ["boxes_a", "boxes_b"],
            "split_unit": "match_id",
            "adjudication": adjudicate,
        },
        {
            "name": "idf1",
            "direction": "maximize",
            "adoption_floor": 0.005,
            "operating_point": operating,
            "aggregation": aggregation,
            "scoring_scope": "cross_corpus",
            "scoring_corpora": ["ident_a"],
            "split_unit": "match_id",
            "adjudication": adjudicate,
        },
    ]
    spec["corpora"] = [
        {"id": "boxes_a", "roles": ["scoring"], "split_unit": "match_id"},
        {"id": "boxes_b", "roles": ["scoring"], "split_unit": "match_id"},
        {"id": "ident_a", "roles": ["scoring"], "split_unit": "match_id"},
    ]
    corpora = {
        "boxes_a": [{"match_id": "m1", "ap50": 0.5}, {"match_id": "m2", "ap50": 0.6}],
        "boxes_b": [{"match_id": "m1", "ap50": 0.4}, {"match_id": "m2", "ap50": 0.7}],
        "ident_a": [{"match_id": "m1", "idf1": 0.8}, {"match_id": "m2", "idf1": 0.9}],
    }
    registry = Registry(tmp_path)
    assert registry.register_campaign_spec(spec, scoring_corpora=corpora)
    rope = registry.list_events()[-1].payload["rope"]
    assert rope["method"] == "vector"
    assert set(rope["metrics"]) == {"ap50", "idf1"}
