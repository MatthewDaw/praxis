"""R1 acceptance: category schema validation for the registry's three fact kinds."""

from __future__ import annotations

import pytest

from knowledge.ml_registry.schema import (
    IDEA,
    MODEL,
    REQUIRED_META_KEYS,
    TRIAL,
    RegistryValidationError,
    validate_fact,
)

WELL_FORMED = {
    MODEL: {
        "metric": "val_bpb",
        "direction": "minimize",
        "win_condition": "beats baseline by the rope",
        "baseline": "commit-abc123",
        "baseline_throughput": 1200,
        "diff_size_limit": 800,
    },
    IDEA: {
        "model_id": "model-1",
        "origin": "seeded",
        "axis": "architecture",
        "description": "try RoPE scaling",
    },
    TRIAL: {
        "model_id": "model-1",
        "idea_id": "idea-1",
        "commit": "deadbeef",
        "status": "running",
    },
}


@pytest.mark.parametrize("category", [MODEL, IDEA, TRIAL])
def test_well_formed_fact_is_accepted(category):
    """A well-formed fact for each of the three categories is accepted."""
    validate_fact(category, WELL_FORMED[category])  # must not raise


@pytest.mark.parametrize("category", [MODEL, IDEA, TRIAL])
def test_fact_missing_a_required_key_is_rejected_naming_it(category):
    """A fact missing any required meta key for its category is rejected naming the key."""
    for missing_key in REQUIRED_META_KEYS[category]:
        meta = {k: v for k, v in WELL_FORMED[category].items() if k != missing_key}
        with pytest.raises(RegistryValidationError) as excinfo:
            validate_fact(category, meta)
        assert excinfo.value.field == missing_key
        assert missing_key in str(excinfo.value)


def test_unknown_category_is_rejected():
    with pytest.raises(RegistryValidationError):
        validate_fact("not-a-real-category", {})


def test_blank_required_value_is_treated_as_missing():
    meta = dict(WELL_FORMED[MODEL])
    meta["metric"] = ""
    with pytest.raises(RegistryValidationError) as excinfo:
        validate_fact(MODEL, meta)
    assert excinfo.value.field == "metric"
