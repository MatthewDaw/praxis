from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from knowledge.ml_registry.survey import (
    OpenAlexClient,
    TechniquePoolError,
    load_technique_pool,
    survey_campaign,
)


def _openalex_page(count: int = 10) -> Mapping[str, Any]:
    return {
        "results": [
            {
                "id": f"https://openalex.org/W{index}",
                "display_name": f"Technique {index}",
                "primary_location": {
                    "landing_page_url": f"https://example.test/papers/{index}",
                },
            }
            for index in range(count)
        ],
    }


def _transferability(work: object) -> Mapping[str, str]:
    return {
        "proven_where": "large-scale image classification",
        "how_it_differs": "our campaign has fewer labels and stronger class imbalance",
        "mechanism": "the regularizer reduces majority-class overconfidence",
    }


def test_campaign_yields_complete_vetted_technique_pool() -> None:
    client = OpenAlexClient(fetch_json=lambda _url: _openalex_page())

    pool = survey_campaign("campaign-7", ["class imbalance"], client, _transferability)

    assert pool.complete is True
    assert len(pool.techniques) == 10
    assert pool.failures == ()
    assert {entry.proven_where for entry in pool.techniques}
    assert {entry.how_it_differs for entry in pool.techniques}
    assert {entry.mechanism for entry in pool.techniques}


def test_loader_rejects_missing_mechanism_at_the_boundary() -> None:
    entry = {
        "id": "W1",
        "title": "Promising method",
        "source_url": "https://example.test/W1",
        "proven_where": "another domain",
        "how_it_differs": "different labels",
    }

    with pytest.raises(TechniquePoolError, match="mechanism"):
        load_technique_pool("campaign-7", [entry])


def test_retrieval_outage_is_recorded_and_never_marked_complete() -> None:
    def unavailable(_url: str) -> Mapping[str, Any]:
        raise TimeoutError("OpenAlex timed out")

    pool = survey_campaign(
        "campaign-7", ["class imbalance"], OpenAlexClient(fetch_json=unavailable),
        _transferability,
    )

    assert pool.complete is False
    assert pool.techniques == ()
    assert len(pool.failures) == 1
    assert pool.failures[0].provider == "openalex"
    assert pool.failures[0].query == "class imbalance"
    assert "timed out" in pool.failures[0].reason
    assert pool.to_dict()["status"] == "failed"
