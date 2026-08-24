"""A run the loop marked UNFAIR is voided, not adjudicated.

`results.tsv` carries a `status` column so the trainer can say a number was not produced under
the conditions the arm was meant to be measured under. `budget_exhausted` marks a run cut short by
wall clock. Scoring an under-trained model as a REJECTION records a settled answer to a question
that was never asked, and a rejection is exactly what a future session reads as settled.

Observed on the first campaign to run a genuinely expensive arm: a graph model was cut off by the
budget, its per-seed scores degrading 0.618 / 0.627 / 0.412 / 0.049 as it diverged, and the
registry scored the truncated mean as a -0.2766 rejection of the entire model family. LedgerRow
did not carry `status` at all, so this was structurally invisible.
"""

from __future__ import annotations

from knowledge.ml_registry.verdict import (FAIR_RUN_STATUSES, LedgerRow, VERDICT_VOIDED,
                                           adjudicate_verdict)
from knowledge.ml_registry.write_path import (RegistrySpace, register_idea, register_model,
                                              register_trial)

from knowledge.ml_registry.testing.rope_fixtures import rope_ledger_rows

#: Four rows measuring a rope of exactly 0.01, at this campaign's baseline level.
ROPE_ROWS = rope_ledger_rows(0.01, at=0.7, throughput=3.38)

META = {"metric": "f1", "direction": "maximize", "win_condition": "beats baseline by the rope",
        "baseline": "base", "baseline_runs": list(ROPE_ROWS),
        "baseline_throughput": 3.38,
        "diff_size_limit": 8, "max_trials": 9, "max_discovered_ideas": 2}


def _setup(status: str, throughput: float = 3.40
           ) -> tuple[RegistrySpace, str, dict[str, LedgerRow]]:
    """Throughput defaults ABOVE the speed gate (3.38 * 0.95 = 3.211) so these tests isolate the
    STATUS check. The real M06 row was also slow, which would have voided it either way -- but for
    a different reason, and conflating the two would leave the status path untested."""
    space = RegistrySpace()
    mid = register_model(space, dict(META))
    iid = register_idea(space, {"model_id": mid, "origin": "seeded", "axis": "architecture",
                                "description": "a graph head"})
    ledger = {"base": LedgerRow(value=0.7034, throughput=3.38, diff_lines=0),
              "c1": LedgerRow(value=0.4268, throughput=throughput, diff_lines=1, status=status),
              **ROPE_ROWS}
    tid = register_trial(space, {"model_id": mid, "idea_id": iid, "commit": "c1",
                                 "status": "complete", "throughput": throughput, "diff_lines": 1},
                         frozenset(ledger))
    return space, tid, ledger


def test_a_budget_exhausted_run_is_voided_not_rejected() -> None:
    space, tid, ledger = _setup("budget_exhausted")
    assert adjudicate_verdict(space, tid, ledger) == VERDICT_VOIDED
    assert "budget_exhausted" in space.get(tid).meta["void_reason"]


def test_the_void_reason_names_the_status() -> None:
    """A void with no reason is indistinguishable from a throughput void, and they mean
    different things: one says re-run, the other says the machine was busy."""
    space, tid, ledger = _setup("diverged")
    adjudicate_verdict(space, tid, ledger)
    assert "diverged" in space.get(tid).meta["void_reason"]


def test_an_ok_run_is_adjudicated_normally() -> None:
    space, tid, ledger = _setup("ok")
    assert adjudicate_verdict(space, tid, ledger) != VERDICT_VOIDED


def test_status_is_checked_BEFORE_throughput() -> None:
    """A slow, truncated run must void for being TRUNCATED. The two voids mean different things --
    one says the arm needs re-running, the other says the machine was busy -- and the real M06 row
    was both, so ordering decides which reason gets recorded."""
    space, tid, ledger = _setup("budget_exhausted", throughput=1.10)
    assert adjudicate_verdict(space, tid, ledger) == VERDICT_VOIDED
    assert "budget_exhausted" in space.get(tid).meta["void_reason"]


def test_a_ledger_without_a_status_column_behaves_exactly_as_before() -> None:
    """Older ledgers are older, not broken. LedgerRow defaults status to 'ok'."""
    assert LedgerRow(value=0.7, throughput=3.4, diff_lines=1).status in FAIR_RUN_STATUSES
    space, tid, _ = _setup("ok")
    ledger = {"base": LedgerRow(value=0.7034, throughput=3.38, diff_lines=0),
              "c1": LedgerRow(value=0.72, throughput=3.40, diff_lines=1),
              **ROPE_ROWS}
    assert adjudicate_verdict(space, tid, ledger) != VERDICT_VOIDED
