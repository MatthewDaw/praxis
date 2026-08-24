"""R5 acceptance: discovered proposals are pool-backed and ablation-conditioned."""

from typing import Optional

from knowledge.ml_registry.supervisor import Dispatcher, PoolIdeaGenerator, dispatch_trial
from knowledge.ml_registry.survey import TechniquePool, load_technique_pool
from knowledge.ml_registry.testing.rope_fixtures import rope_ledger_rows
from knowledge.ml_registry.verdict import LedgerRow
from knowledge.ml_registry.write_path import Fact, RegistrySpace, register_idea, register_model


BASELINE = "baseline"
ROPE_ROWS = rope_ledger_rows(0.01, at=1.0, throughput=1200)
LEDGER = {
    BASELINE: LedgerRow(value=1.0, throughput=1200, diff_lines=0),
    **ROPE_ROWS,
    **{
        f"loss-{index}": LedgerRow(value=2.0, throughput=1200, diff_lines=10)
        for index in range(1, 5)
    },
}


def _space() -> tuple[RegistrySpace, str]:
    space = RegistrySpace()
    model_id = register_model(space, {
        "metric": "error",
        "direction": "minimize",
        "win_condition": "beats baseline by the rope",
        "win_on_adoption_ok": True,
        "baseline": BASELINE,
        "baseline_runs": list(ROPE_ROWS),
        "baseline_throughput": 1200,
        "diff_size_limit": 100,
        "max_trials": 10,
        "max_discovered_ideas": 4,
    })
    return space, model_id


def _pool(count: int = 2) -> TechniquePool:
    return load_technique_pool("campaign-r5", [
        {
            "id": f"technique-{index}",
            "title": f"Technique {index}",
            "source_url": f"https://example.test/{index}",
            "proven_where": "image classification",
            "how_it_differs": "the target corpus is video",
            "mechanism": "regularizes the learned representation",
        }
        for index in range(1, count + 1)
    ], minimum_size=1)


def _loss(commit: str, **evidence: object) -> Dispatcher:
    def dispatch(space: RegistrySpace, model: Fact, idea: Fact) -> dict[str, object]:
        return {"commit": commit, **evidence}

    return dispatch


def test_pool_proposals_cite_entries_and_follow_the_latest_ablation_target() -> None:
    space, model_id = _space()
    generator = PoolIdeaGenerator(_pool(), default_target_block="input_pipeline")

    first = dispatch_trial(
        space, model_id, LEDGER, _loss("loss-1"), idea_generator=generator,
    )
    first_idea = space.get(str(first["candidate"]))
    assert first_idea is not None
    assert first_idea.meta["proposal_origin"] == "technique_pool"
    assert first_idea.meta["technique_id"] == "technique-1"
    assert first_idea.meta["target_block"] == "input_pipeline"

    register_idea(space, {
        "model_id": model_id,
        "origin": "seeded",
        "axis": "ablation",
        "description": "locate the next high-leverage block",
    })
    dispatch_trial(
        space,
        model_id,
        LEDGER,
        _loss("loss-2", ablation_result={"target_block": "encoder"}),
    )

    conditioned = dispatch_trial(
        space, model_id, LEDGER, _loss("loss-3"), idea_generator=generator,
    )
    conditioned_idea = space.get(str(conditioned["candidate"]))
    assert conditioned_idea is not None
    assert conditioned_idea.meta["technique_id"] == "technique-2"
    assert conditioned_idea.meta["target_block"] == "encoder"
    assert conditioned_idea.meta["basis"] == "technique_pool:technique-2"


def test_fallback_proposals_are_durably_marked_as_outside_the_pool() -> None:
    space, model_id = _space()

    def model_prior(
        space: RegistrySpace,
        model_id: str,
        forced_axis: Optional[str],
        permitted_axes: frozenset[str],
    ) -> Optional[dict[str, object]]:
        return {"axis": "optimizer", "description": "invent a new schedule", "basis": "model_prior"}

    generator = PoolIdeaGenerator(
        _pool(count=1), default_target_block="optimizer", outside_pool_generator=model_prior,
    )
    dispatch_trial(space, model_id, LEDGER, _loss("loss-1"), idea_generator=generator)
    fallback = dispatch_trial(space, model_id, LEDGER, _loss("loss-2"), idea_generator=generator)

    fallback_idea = space.get(str(fallback["candidate"]))
    assert fallback_idea is not None
    assert fallback_idea.meta["proposal_origin"] == "outside_pool"
    assert fallback_idea.meta["outside_pool_reason"] == "technique_pool_exhausted"
    assert "technique_id" not in fallback_idea.meta
