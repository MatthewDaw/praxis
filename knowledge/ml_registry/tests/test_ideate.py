"""R6 acceptance: af-ml-ideate seeds a model's starting idea set by sweeping the nine-axis
closed set (six generative, three retrieval) rather than trying to enumerate an exhaustive
plan.

Covers, directly against :mod:`knowledge.ml_registry.ideate`:

* a run against a fixture model writes at least one idea per generative axis, every written
  idea carrying ``origin="seeded"`` and a ``meta.axis`` drawn from the nine-value closed set.
* batch mode (:func:`~knowledge.ml_registry.ideate.always_confirm`) and interactive mode (a
  human-backed confirmer) write ideas of IDENTICAL shape -- only which candidates get through
  differs.
* each of the three retrieval axes records an execution receipt naming the query issued, the
  count returned, and the ids retrieved -- even an axis that legitimately retrieves nothing.
* the run is not required to enumerate every idea the loop will eventually try (an axis whose
  generator/retriever proposes nothing still completes the sweep).
"""

from __future__ import annotations

import pytest

from knowledge.ml_registry.ideate import (
    GENERATIVE_AXES,
    IDEATION_AXES,
    RETRIEVAL_AXES,
    RetrievalResult,
    always_confirm,
    seed_campaign,
    sweep_generative_axes,
    sweep_retrieval_axes,
)
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.write_path import SEEDED, RegistrySpace, register_model

MODEL_META = {
    "metric": "val_bpb",
    "direction": "minimize",
    "win_condition": "beats baseline by noise_floor",
    "baseline": "commit-abc123",
    "noise_floor": 0.01,
    "baseline_throughput": 1200,
    "diff_size_limit": 800,
}


def _space_with_model() -> tuple[RegistrySpace, str]:
    space = RegistrySpace()
    model_id = register_model(space, MODEL_META)
    return space, model_id


def _one_candidate_per_axis(axis: str, model_meta: dict[str, object]) -> list[dict[str, object]]:
    return [{"description": f"{axis} candidate for {model_meta['metric']}"}]


def _empty_generator(axis: str, model_meta: dict[str, object]) -> list[dict[str, object]]:
    return []


def _fixture_retriever(axis: str, model_meta: dict[str, object]) -> RetrievalResult:
    if axis == "current_code":
        return RetrievalResult(
            query="grep TODO in train.py",
            rows=({"id": "train.py:42", "description": "revisit the warmup schedule"},),
        )
    if axis == "prior_trials":
        return RetrievalResult(
            query="registry: trials for sibling models",
            rows=({"id": "trial-abc", "description": "retry the sibling model's winning idea"},),
        )
    if axis == "af_learn_lessons":
        # a retrieval axis that legitimately finds nothing still records its receipt.
        return RetrievalResult(query="af-learn: lessons tagged ml-research", rows=())
    raise AssertionError(f"unexpected retrieval axis {axis!r}")


def test_seed_campaign_writes_at_least_one_idea_per_generative_axis_all_seeded_and_closed_axis():
    space, model_id = _space_with_model()
    run = seed_campaign(
        space, model_id, generator=_one_candidate_per_axis, retriever=_fixture_retriever,
    )

    ideas = space.list_facts("idea")
    assert ideas, "seeding must write at least one idea"
    for idea in ideas:
        assert idea.meta["origin"] == SEEDED
        assert idea.meta["axis"] in IDEATION_AXES

    for axis in GENERATIVE_AXES:
        assert run.written[axis], f"axis {axis!r} must yield at least one written idea"
        axis_ideas = [i for i in ideas if i.meta["axis"] == axis]
        assert len(axis_ideas) >= 1


def test_batch_and_interactive_modes_write_ideas_of_identical_shape():
    batch_space, batch_model = _space_with_model()
    batch_run = seed_campaign(
        batch_space, batch_model, generator=_one_candidate_per_axis, retriever=_fixture_retriever,
        confirm=always_confirm,
    )

    def confirm_everything(axis: str, candidate: dict[str, object]) -> bool:
        return True  # stands in for a human who confirms every offered candidate

    interactive_space, interactive_model = _space_with_model()
    interactive_run = seed_campaign(
        interactive_space, interactive_model, generator=_one_candidate_per_axis,
        retriever=_fixture_retriever, confirm=confirm_everything,
    )

    batch_ideas = sorted(
        (i.meta["axis"], i.meta["origin"], i.meta["description"]) for i in batch_space.list_facts("idea")
    )
    interactive_ideas = sorted(
        (i.meta["axis"], i.meta["origin"], i.meta["description"])
        for i in interactive_space.list_facts("idea")
    )
    assert batch_ideas == interactive_ideas
    assert batch_run.written.keys() == interactive_run.written.keys()

    def declines_everything(axis: str, candidate: dict[str, object]) -> bool:
        return False

    declined_space, declined_model = _space_with_model()
    declined_run = seed_campaign(
        declined_space, declined_model, generator=_one_candidate_per_axis,
        retriever=_fixture_retriever, confirm=declines_everything,
    )
    assert declined_space.list_facts("idea") == []
    assert all(ids == [] for ids in declined_run.written.values())


def test_each_retrieval_axis_records_an_execution_receipt_naming_query_count_and_ids():
    space, model_id = _space_with_model()
    run = seed_campaign(space, model_id, generator=_one_candidate_per_axis, retriever=_fixture_retriever)

    receipts_by_axis = {r.axis: r for r in run.receipts}
    assert set(receipts_by_axis) == set(RETRIEVAL_AXES)

    current_code = receipts_by_axis["current_code"]
    assert current_code.query == "grep TODO in train.py"
    assert current_code.count == 1
    assert current_code.ids == ("train.py:42",)

    prior_trials = receipts_by_axis["prior_trials"]
    assert prior_trials.count == 1
    assert prior_trials.ids == ("trial-abc",)

    # an axis that legitimately retrieves nothing still records its receipt (count=0, ids=()).
    lessons = receipts_by_axis["af_learn_lessons"]
    assert lessons.query == "af-learn: lessons tagged ml-research"
    assert lessons.count == 0
    assert lessons.ids == ()


def test_run_is_not_required_to_enumerate_every_idea_the_loop_will_eventually_try():
    """An axis whose generator/retriever proposes nothing this pass still completes the sweep
    -- ideation seeds a starting set, not an exhaustive plan."""
    space, model_id = _space_with_model()

    def empty_retriever(axis: str, model_meta: dict[str, object]) -> RetrievalResult:
        return RetrievalResult(query=f"query for {axis}", rows=())

    run = seed_campaign(space, model_id, generator=_empty_generator, retriever=empty_retriever)

    assert space.list_facts("idea") == []
    assert set(run.written) == set(IDEATION_AXES)
    assert all(ids == [] for ids in run.written.values())
    assert {r.axis for r in run.receipts} == set(RETRIEVAL_AXES)
    assert all(r.count == 0 for r in run.receipts)


def test_seed_campaign_refuses_an_unregistered_model_naming_it():
    space = RegistrySpace()
    with pytest.raises(RegistryValidationError) as exc_info:
        seed_campaign(space, "model-nope", generator=_one_candidate_per_axis, retriever=_fixture_retriever)
    assert exc_info.value.field == "model_id"


def test_sweep_generative_axes_can_be_scoped_to_a_subset_of_axes():
    space, model_id = _space_with_model()
    written = sweep_generative_axes(
        space, model_id, MODEL_META, _one_candidate_per_axis, axes=("ablation",),
    )
    assert set(written) == {"ablation"}
    ideas = space.list_facts("idea")
    assert len(ideas) == 1
    assert ideas[0].meta["axis"] == "ablation"


def test_sweep_retrieval_axes_returns_receipts_even_when_scoped_to_one_axis():
    space, model_id = _space_with_model()
    written, receipts = sweep_retrieval_axes(
        space, model_id, MODEL_META, _fixture_retriever, axes=("current_code",),
    )
    assert set(written) == {"current_code"}
    assert [r.axis for r in receipts] == ["current_code"]
