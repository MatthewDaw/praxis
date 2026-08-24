"""R6 acceptance: a stuck stage stops after ten experiments and refreshes its pool."""

from knowledge.ml_registry.contracts import StageCloseRecord, StageOutcome
from knowledge.ml_registry.lifecycle import untried_backlog
from knowledge.ml_registry.supervisor import (
    CLOSE_STAGNATION,
    DEFAULT_STAGNATION_LIMIT,
    STAGE_CLOSES_FIELD,
    supervise_campaign,
)
from knowledge.ml_registry.testing.rope_fixtures import rope_ledger_rows
from knowledge.ml_registry.verdict import LedgerRow
from knowledge.ml_registry.write_path import (
    Fact,
    RegistrySpace,
    register_idea,
    register_model,
)


BASELINE = "baseline"
ROPE_ROWS = rope_ledger_rows(0.01, at=1.0, throughput=1200)

#: Every arm regresses the metric, so no dispatch against it can ever clear the rope.
LOSING_LEDGER = {
    BASELINE: LedgerRow(value=1.0, throughput=1200, diff_lines=0),
    **ROPE_ROWS,
    **{
        f"loss-{index}": LedgerRow(value=2.0, throughput=1200, diff_lines=10)
        for index in range(1, 11)
    },
}


def _space() -> tuple[RegistrySpace, str]:
    space = RegistrySpace()
    model_id = register_model(space, {
        "metric": "error",
        "direction": "minimize",
        "win_condition": {"metric_at_most": 0.0},
        "baseline": BASELINE,
        "baseline_runs": list(ROPE_ROWS),
        "baseline_throughput": 1200,
        "diff_size_limit": 100,
        "max_trials": 100,
        "max_discovered_ideas": 20,
        "max_consecutive_voids": 20,
    })
    for index in range(1, 12):
        register_idea(space, {
            "model_id": model_id,
            "origin": "seeded",
            "axis": "architecture",
            "stage": "architecture",
            "description": f"unclearable arm {index}",
        })
    return space, model_id


def test_unclearable_stage_stops_at_ten_and_refreshes_outside_the_loop() -> None:
    space, model_id = _space()
    commits = iter(["voided", *(f"loss-{index}" for index in range(1, 11))])
    # A throughput regression the rope refuses to measure -- the ledger voids this arm.
    ledger = {**LOSING_LEDGER, "voided": LedgerRow(value=0.5, throughput=1000, diff_lines=10)}
    dispatches: list[str] = []

    def dispatch(space: RegistrySpace, model: Fact, idea: Fact) -> dict[str, object]:
        commit = next(commits)
        dispatches.append(commit)
        return {"commit": commit}

    refresh_calls: list[tuple[str, str]] = []

    def refresh(campaign_id: str, stage: str) -> object:
        # The refresher is the network-enabled pass. Seeing all ten dispatches here proves
        # the sealed iteration loop has stopped before retrieval begins.
        assert len(dispatches) == DEFAULT_STAGNATION_LIMIT
        refresh_calls.append((campaign_id, stage))
        return {"pool_version": "refreshed-v2"}

    result = supervise_campaign(
        space,
        model_id,
        ledger,
        dispatch,
        pool_refresher=refresh,
    )

    assert len(result["history"]) == DEFAULT_STAGNATION_LIMIT == 10
    assert result["close"] == CLOSE_STAGNATION
    # Two of the eleven arms never ran -- the one the stop pre-empted, and the one whose
    # trial was ledger-voided (voided arms advance the counter but stay untried), so the
    # close is EARLY and its reason may not claim the stage was exhausted.
    assert result["close_detail"] == {
        "stage": "architecture",
        "outcome": "STAGNANT",
        "reason": "no improvement in the last 10 experiments; "
                  "2 of 11 material families untried",
        "experiments_without_improvement": 10,
        "limit": 10,
    }
    assert result["history"][0]["status"] == "voided"
    assert result["pool_refresh"] == {"pool_version": "refreshed-v2"}
    assert refresh_calls == [(model_id, "architecture")]
    model = space.get(model_id)
    assert model is not None
    assert model.meta[STAGE_CLOSES_FIELD] == [result["close_detail"]]


def test_the_persisted_stage_close_validates_against_the_shared_stage_contract() -> None:
    """The merged seam: R6's early stop and R3b's typed stage outcome are ONE record.

    The stop fires with an untried arm still in the stage, which
    :meth:`StageOutcome.for_stage` rejects as "still open" unless the close is forced --
    so the record's own constructor is the only thing that may author it.
    """
    space, model_id = _space()
    commits = iter(f"loss-{index}" for index in range(1, 11))

    def dispatch(space: RegistrySpace, model: Fact, idea: Fact) -> dict[str, object]:
        return {"commit": next(commits)}

    result = supervise_campaign(space, model_id, LOSING_LEDGER, dispatch)

    assert result["close"] == CLOSE_STAGNATION
    untried = [idea for idea in untried_backlog(space, model_id=model_id)
               if idea.meta.get("stage") == "architecture"]
    assert len(untried) == 1, "the stop must pre-empt a remaining arm, not exhaust the stage"

    record = StageCloseRecord.from_mapping(
        space.get(model_id).meta[STAGE_CLOSES_FIELD][0],  # type: ignore[union-attr]
    )
    assert record.outcome is StageOutcome.STAGNANT
    assert record.reason == ("no improvement in the last 10 experiments; "
                             "1 of 11 material families untried")
    assert record.reason != StageOutcome.STAGNANT.reason
    assert (record.experiments_without_improvement, record.limit) == (10, 10)


def test_an_improvement_resets_the_stage_stagnation_window() -> None:
    space, model_id = _space()
    ledger = {**LOSING_LEDGER, "win": LedgerRow(value=0.5, throughput=1200, diff_lines=10)}
    commits = iter([*(f"loss-{index}" for index in range(1, 6)), "win"])

    def dispatch(space: RegistrySpace, model: Fact, idea: Fact) -> dict[str, object]:
        return {"commit": next(commits)}

    result = supervise_campaign(space, model_id, ledger, dispatch, max_dispatches=6)

    assert result["close"] is None
    assert len(result["history"]) == 6
