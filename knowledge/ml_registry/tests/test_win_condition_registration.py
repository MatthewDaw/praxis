"""R11 acceptance: campaign-budget defaults and the metric freeze on model registration.

Builds on R1's schema (:mod:`knowledge.ml_registry.schema`) and R2's write path
(:mod:`knowledge.ml_registry.write_path`). A model registration still requires the seven
judging fields R1 fixed (metric, direction, win_condition, baseline,
baseline_throughput, diff_size_limit) -- unchanged, still refused by name when any is
missing. What R11 adds:

* three campaign-budget fields the caller MAY omit, each adopting a fixed default when it
  is: ``per_trial_seconds`` -> 420, ``max_trials`` -> 200, ``max_discovered_ideas`` -> 8.
* every registration -- defaulted or not -- still runs through the registry validator
  (:func:`knowledge.ml_registry.schema.validate_fact`).
* a model's ``metric`` is frozen for its life: a later re-registration against the same
  ``model_id`` that tries to change ``metric`` is refused naming it, even though other
  fields may be updated.
"""

from __future__ import annotations

import pytest

from knowledge.ml_registry.schema import MODEL, RegistryValidationError
from knowledge.ml_registry.write_path import (
    MODEL_DEFAULTS,
    Fact,
    RegistrySpace,
    register_model,
)


def _require(space: RegistrySpace, fact_id: str) -> Fact:
    """``RegistrySpace.get`` returns ``Fact | None``; narrow it once here rather than
    dereferencing the optional at every call site."""
    fact = space.get(fact_id)
    assert fact is not None, f"fact {fact_id!r} was not registered"
    return fact

MODEL_META: dict[str, object] = {
    "metric": "val_bpb",
    "direction": "minimize",
    "win_condition": "beats baseline by the rope",
    "baseline": "commit-abc123",
    "baseline_throughput": 1200,
    "diff_size_limit": 800,
}

REQUIRED_FIELDS = (
    "metric",
    "direction",
    "win_condition",
    "baseline",
    "baseline_throughput",
    "diff_size_limit",
)


@pytest.mark.parametrize("missing_field", REQUIRED_FIELDS)
def test_registration_missing_a_required_field_is_refused_naming_it(missing_field: str) -> None:
    space = RegistrySpace()
    meta = {k: v for k, v in MODEL_META.items() if k != missing_field}
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model(space, meta)
    assert excinfo.value.field == missing_field


@pytest.mark.parametrize("budget_field,default", sorted(MODEL_DEFAULTS.items()))
def test_registration_omitting_a_budget_field_adopts_its_default(
    budget_field: str, default: object
) -> None:
    """``MODEL_META`` already omits every :data:`MODEL_DEFAULTS` field, so registering it
    as-is exercises the default path for each in turn."""
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))
    assert _require(space, model_id).meta[budget_field] == default


def test_registration_honors_an_explicit_budget_over_the_default() -> None:
    space = RegistrySpace()
    meta: dict[str, object] = dict(MODEL_META)
    meta["per_trial_seconds"] = 90
    meta["max_trials"] = 5
    meta["max_discovered_ideas"] = 1
    model_id = register_model(space, meta)
    got = _require(space, model_id).meta
    assert got["per_trial_seconds"] == 90
    assert got["max_trials"] == 5
    assert got["max_discovered_ideas"] == 1


def test_model_defaults_constant_matches_the_documented_budget() -> None:
    assert MODEL_DEFAULTS == {"per_trial_seconds": 420, "max_trials": 200, "max_discovered_ideas": 8}


def test_re_registering_a_model_with_a_different_metric_is_refused_naming_it() -> None:
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))
    changed: dict[str, object] = dict(MODEL_META)
    changed["metric"] = "throughput"
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model(space, changed, model_id=model_id)
    assert excinfo.value.field == "metric"
    # the original registration is untouched
    assert _require(space, model_id).meta["metric"] == "val_bpb"


def test_re_registering_a_model_with_the_same_metric_updates_other_fields_in_place() -> None:
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))
    updated: dict[str, object] = dict(MODEL_META)
    updated["baseline_throughput"] = 1500
    got_id = register_model(space, updated, model_id=model_id)
    assert got_id == model_id
    registered = _require(space, model_id)
    assert registered.meta["baseline_throughput"] == 1500
    assert registered.category == MODEL


def test_re_registering_an_unknown_model_id_is_refused_naming_it() -> None:
    space = RegistrySpace()
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model(space, dict(MODEL_META), model_id="model-does-not-exist")
    assert excinfo.value.field == "model_id"
