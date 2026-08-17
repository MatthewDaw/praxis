"""R2 acceptance: the registry write API (model / idea / trial registration)."""

from __future__ import annotations

import pytest

from knowledge.ml_registry.citation import ResolvedCitation, ResolverUnreachable
from knowledge.ml_registry.schema import IDEA, MODEL, TRIAL, RegistryValidationError
from knowledge.ml_registry.write_path import (
    RegistrySpace,
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


def test_unbudgeted_model_allows_unlimited_discovered_ideas():
    space = RegistrySpace()
    meta = dict(MODEL_META)
    del meta["max_discovered_ideas"]
    model_id = register_model(space, meta)
    for _ in range(5):
        register_idea(space, _idea_meta(model_id, origin="discovered"))  # must not raise


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
