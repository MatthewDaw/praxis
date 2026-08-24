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
