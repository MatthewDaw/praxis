"""R6 acceptance: a stuck stage stops after ten experiments and refreshes its pool."""

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
    ledger = {
        BASELINE: LedgerRow(value=1.0, throughput=1200, diff_lines=0),
        **ROPE_ROWS,
        "voided": LedgerRow(value=0.5, throughput=1000, diff_lines=10),
        **{
            f"loss-{index}": LedgerRow(value=2.0, throughput=1200, diff_lines=10)
            for index in range(1, 11)
        },
    }
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
    assert result["close_detail"] == {
        "stage": "architecture",
        "reason": "no improvement in the last 10 experiments",
        "experiments_without_improvement": 10,
        "limit": 10,
    }
    assert result["history"][0]["status"] == "voided"
    assert result["pool_refresh"] == {"pool_version": "refreshed-v2"}
    assert refresh_calls == [(model_id, "architecture")]
    model = space.get(model_id)
    assert model is not None
    assert model.meta[STAGE_CLOSES_FIELD] == [result["close_detail"]]


def test_an_improvement_resets_the_stage_stagnation_window() -> None:
    space, model_id = _space()
    ledger = {
        BASELINE: LedgerRow(value=1.0, throughput=1200, diff_lines=0),
        **ROPE_ROWS,
        "win": LedgerRow(value=0.5, throughput=1200, diff_lines=10),
        **{
            f"loss-{index}": LedgerRow(value=2.0, throughput=1200, diff_lines=10)
            for index in range(1, 11)
        },
    }
    commits = iter([*(f"loss-{index}" for index in range(1, 6)), "win"])

    def dispatch(space: RegistrySpace, model: Fact, idea: Fact) -> dict[str, object]:
        return {"commit": next(commits)}

    result = supervise_campaign(space, model_id, ledger, dispatch, max_dispatches=6)

    assert result["close"] is None
    assert len(result["history"]) == 6
