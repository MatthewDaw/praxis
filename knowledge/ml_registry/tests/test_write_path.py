"""R2 acceptance: the registry write API (model / idea / trial registration)."""

from __future__ import annotations

import pytest

from knowledge.ml_registry.citation import ResolvedCitation, ResolverUnreachable
from knowledge.ml_registry.schema import IDEA, MODEL, TRIAL, RegistryValidationError
from knowledge.ml_registry.write_path import (
    DEFAULT_MAX_DISCOVERED_IDEAS,
    UNLIMITED_DISCOVERED_IDEAS,
    RegistrySpace,
    mutate_model,
    register_idea,
    register_model,
    register_trial,
    resolve_idea_citation,
)

MODEL_META = {
    "metric": "val_bpb",
    "direction": "minimize",
    "win_condition": "beats baseline by noise_floor",
    "baseline": "commit-abc123",
    "noise_floor": 0.01,
    "baseline_throughput": 1200,
    "diff_size_limit": 800,
    "max_discovered_ideas": 1,
}

LEDGER = frozenset({"deadbeef", "feedface"})


def _idea_meta(model_id: str, *, origin: str = "seeded") -> dict[str, object]:
    return {
        "model_id": model_id,
        "origin": origin,
        "axis": "architecture",
        "description": "try RoPE scaling",
    }


def _trial_meta(model_id: str, idea_id: str, *, commit: str = "deadbeef") -> dict[str, object]:
    return {"model_id": model_id, "idea_id": idea_id, "commit": commit, "status": "running"}


def test_register_model_refuses_a_zero_noise_floor():
    """register_model_with_baseline already refused this; the plain write path did not,
    which is the path every test fixture and C1 actually use."""
    space = RegistrySpace()
    with pytest.raises(RegistryValidationError) as excinfo:
        register_model(space, dict(MODEL_META, noise_floor=0.0))
    assert excinfo.value.field == "noise_floor"


def test_register_model_idea_trial_readback_returns_all_three_and_trial_derives_from_idea():
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = register_trial(space, _trial_meta(model_id, idea_id), LEDGER)

    facts = {f.id: f for f in space.list_facts()}
    assert set(facts) == {model_id, idea_id, trial_id}
    assert facts[model_id].category == MODEL
    assert facts[idea_id].category == IDEA
    assert facts[trial_id].category == TRIAL
    assert facts[trial_id].derived_from == (idea_id,)


@pytest.mark.parametrize("origin", ["seeded", "discovered"])
def test_every_idea_carries_a_valid_origin(origin):
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))
    idea_id = register_idea(space, _idea_meta(model_id, origin=origin))
    assert space.get(idea_id).meta["origin"] == origin


def test_idea_with_an_invalid_origin_is_refused_naming_it():
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))
    with pytest.raises(RegistryValidationError) as excinfo:
        register_idea(space, _idea_meta(model_id, origin="guessed"))
    assert excinfo.value.field == "origin"


def test_discovered_idea_beyond_the_model_budget_is_refused_naming_the_budget():
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))  # max_discovered_ideas=1
    register_idea(space, _idea_meta(model_id, origin="discovered"))  # fills the budget
    with pytest.raises(RegistryValidationError) as excinfo:
        register_idea(space, _idea_meta(model_id, origin="discovered"))
    assert excinfo.value.field == "max_discovered_ideas"
    assert "max_discovered_ideas" in str(excinfo.value)


def test_discovered_idea_budget_refusal_applies_regardless_of_caller():
    """The budget is enforced no matter which caller made the request -- there is no
    caller-identity bypass, unlike R1's worker-only mutation guards."""
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))
    register_idea(space, _idea_meta(model_id, origin="discovered"))
    for caller_meta in (_idea_meta(model_id, origin="discovered"),):
        caller_meta["source"] = "supervisor"  # any caller identity, not just "worker"
        with pytest.raises(RegistryValidationError) as excinfo:
            register_idea(space, caller_meta)
        assert excinfo.value.field == "max_discovered_ideas"


def _model_fact_without_budget(space: RegistrySpace) -> str:
    """A model fact built WITHOUT register_model, so it carries no max_discovered_ideas
    at all -- the only way the absent-budget path is reachable."""
    meta = dict(MODEL_META)
    del meta["max_discovered_ideas"]
    model_id = space.insert(MODEL, meta)
    assert "max_discovered_ideas" not in space.get(model_id).meta
    return model_id


def test_model_fact_missing_its_budget_field_does_not_get_unlimited_discovered_ideas():
    """A cost control must not fail open: an ABSENT max_discovered_ideas falls back to the
    documented default (8), not to unlimited."""
    space = RegistrySpace()
    model_id = _model_fact_without_budget(space)

    for _ in range(DEFAULT_MAX_DISCOVERED_IDEAS):
        register_idea(space, _idea_meta(model_id, origin="discovered"))

    with pytest.raises(RegistryValidationError) as excinfo:
        register_idea(space, _idea_meta(model_id, origin="discovered"))
    assert excinfo.value.field == "max_discovered_ideas"


def test_the_documented_default_budget_is_finite():
    assert DEFAULT_MAX_DISCOVERED_IDEAS == 8
    assert DEFAULT_MAX_DISCOVERED_IDEAS != UNLIMITED_DISCOVERED_IDEAS


def test_unlimited_discovered_ideas_stays_reachable_via_the_explicit_sentinel():
    space = RegistrySpace()
    meta = dict(MODEL_META)
    meta["max_discovered_ideas"] = UNLIMITED_DISCOVERED_IDEAS
    model_id = register_model(space, meta)
    for _ in range(DEFAULT_MAX_DISCOVERED_IDEAS + 3):
        register_idea(space, _idea_meta(model_id, origin="discovered"))  # must not raise


def test_a_negative_budget_that_is_not_the_sentinel_is_refused_naming_the_field():
    space = RegistrySpace()
    meta = dict(MODEL_META)
    meta["max_discovered_ideas"] = -7
    model_id = register_model(space, meta)
    with pytest.raises(RegistryValidationError) as excinfo:
        register_idea(space, _idea_meta(model_id, origin="discovered"))
    assert excinfo.value.field == "max_discovered_ideas"


def test_worker_sourced_write_path_mutation_of_a_protected_field_is_refused():
    """The guards must sit on the DATA PATH, not only behind the CLI: a direct
    mutate_model call is refused just the same."""
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))

    with pytest.raises(RegistryValidationError) as excinfo:
        mutate_model(space, model_id, {"noise_floor": 99.0}, source="worker")
    assert excinfo.value.field == "noise_floor"
    assert space.get(model_id).meta["noise_floor"] == MODEL_META["noise_floor"]


def test_write_path_baseline_move_from_a_non_adjudication_source_is_refused():
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))

    for source in ("worker", "supervisor", "human"):
        with pytest.raises(RegistryValidationError) as excinfo:
            mutate_model(space, model_id, {"baseline": "commit-def456"}, source=source)
        assert excinfo.value.field == "baseline"
        assert space.get(model_id).meta["baseline"] == MODEL_META["baseline"]


def test_write_path_baseline_move_from_adjudication_is_applied():
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))
    mutate_model(space, model_id, {"baseline": "commit-def456"}, source="adjudication")
    assert space.get(model_id).meta["baseline"] == "commit-def456"


def test_write_path_worker_mutation_of_an_unprotected_field_is_applied():
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))
    mutate_model(space, model_id, {"description": "renamed"}, source="worker")
    assert space.get(model_id).meta["description"] == "renamed"


def test_mutate_model_refuses_an_unregistered_model_naming_it():
    space = RegistrySpace()
    with pytest.raises(RegistryValidationError) as excinfo:
        mutate_model(space, "model-nope", {"description": "x"}, source="adjudication")
    assert excinfo.value.field == "model_id"


def test_trial_referencing_an_unregistered_idea_is_refused_naming_it():
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))
    with pytest.raises(RegistryValidationError) as excinfo:
        register_trial(space, _trial_meta(model_id, "idea-does-not-exist"), LEDGER)
    assert excinfo.value.field == "idea_id"


def test_trial_whose_commit_has_no_ledger_row_is_refused_naming_it():
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))
    idea_id = register_idea(space, _idea_meta(model_id))
    with pytest.raises(RegistryValidationError) as excinfo:
        register_trial(space, _trial_meta(model_id, idea_id, commit="not-in-ledger"), LEDGER)
    assert excinfo.value.field == "commit"


def test_registry_space_round_trips_through_json():
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))
    idea_id = register_idea(space, _idea_meta(model_id))
    trial_id = register_trial(space, _trial_meta(model_id, idea_id), LEDGER)

    reloaded = RegistrySpace.from_json(space.to_json())
    facts = {f.id: f for f in reloaded.list_facts()}
    assert set(facts) == {model_id, idea_id, trial_id}
    assert facts[trial_id].derived_from == (idea_id,)


def test_resolve_idea_citation_records_a_resolved_reference_on_the_idea() -> None:
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))
    idea_id = register_idea(space, _idea_meta(model_id))

    def resolver(reference: str) -> ResolvedCitation | None:
        return ResolvedCitation(title="Attention Is All You Need", authors=("Vaswani",))

    meta = resolve_idea_citation(space, idea_id, "2301.12345", resolver)
    assert meta["basis"] == "external"
    assert meta["title"] == "Attention Is All You Need"
    assert space.get(idea_id).meta["basis"] == "external"


def _no_such_reference(reference: str) -> ResolvedCitation | None:
    return None


def test_resolve_idea_citation_refuses_an_unregistered_idea_naming_it() -> None:
    space = RegistrySpace()
    with pytest.raises(RegistryValidationError) as excinfo:
        resolve_idea_citation(space, "idea-nope", "2301.12345", _no_such_reference)
    assert excinfo.value.field == "idea_id"


def test_resolve_idea_citation_carries_the_unreachable_streak_across_calls() -> None:
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))
    idea_id = register_idea(space, _idea_meta(model_id))

    def unreachable(reference: str) -> ResolvedCitation | None:
        raise ResolverUnreachable(reference)

    for _ in range(2):
        meta = resolve_idea_citation(space, idea_id, "2301.12345", unreachable)
        assert "basis" not in meta
    assert space.get(idea_id).meta["unreachable_streak"] == 2

    meta = resolve_idea_citation(space, idea_id, "2301.12345", unreachable)
    assert meta["basis"] == "reasoned"
    assert meta["unreachable_streak"] == 0
