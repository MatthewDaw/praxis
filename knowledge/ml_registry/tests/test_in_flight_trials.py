"""An idea may have only ONE trial in flight.

The failure this prevents was observed on the first real campaign. Killing a supervising loop left
its training child orphaned to PID 1, still running the PREVIOUS (uncomposed) configuration; the
relaunched campaign started the composed arm under the same idea. Two runs then raced, each taking
half an 8-core box, each about to write a ledger row under the same arm tag. Nothing in either
system objected -- the duplicate was found by reading `ps`, which is not a control.
"""

from __future__ import annotations

import pytest

from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.write_path import (RegistrySpace, register_idea, register_model,
                                              register_trial, supersede_trial)

from knowledge.ml_registry.testing.rope_fixtures import rope_ledger_rows
from knowledge.ml_registry.verdict import LedgerRow

LEDGER = frozenset({"c1", "c2"})

#: Four rows measuring a rope of exactly 0.01, at the fixtures' baseline level.
ROPE_ROWS = rope_ledger_rows(0.01, at=1.0, throughput=1200.0)


MODEL_META = {
    "metric": "f1", "direction": "maximize", "win_condition": "beats baseline by the rope",
    "baseline": "c1", "baseline_throughput": 1.0, "diff_size_limit": 8,
    "max_trials": 5, "max_discovered_ideas": 2,
}


def _space() -> tuple[RegistrySpace, str, str]:
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))
    idea_id = register_idea(space, {"model_id": model_id, "origin": "seeded",
                                    "axis": "representation", "description": "d"})
    return space, model_id, idea_id


def _meta(model_id: str, idea_id: str, commit: str, status: str) -> dict:
    return {"model_id": model_id, "idea_id": idea_id, "commit": commit, "status": status,
            "throughput": 1.0, "diff_lines": 1}


def test_a_second_trial_for_an_in_flight_idea_is_refused() -> None:
    space, model_id, idea_id = _space()
    register_trial(space, _meta(model_id, idea_id, "c1", "complete"), LEDGER)
    with pytest.raises(RegistryValidationError) as exc:
        register_trial(space, _meta(model_id, idea_id, "c2", "complete"), LEDGER)
    # The message must name BOTH commits: the whole difficulty of the original incident was not
    # knowing which configuration the surviving run was measuring.
    assert "c1" in str(exc.value) and "c2" in str(exc.value)


def test_a_resolved_trial_does_not_block_a_later_one() -> None:
    """Retries are legitimate; only CONCURRENCY is not."""
    space, model_id, idea_id = _space()
    register_trial(space, _meta(model_id, idea_id, "c1", "failed"), LEDGER)
    assert register_trial(space, _meta(model_id, idea_id, "c2", "complete"), LEDGER)


def test_a_voided_trial_does_not_block_its_re_run() -> None:
    """Voided MEANS re-run, so it must not wedge the idea it voided on."""
    space, model_id, idea_id = _space()
    register_trial(space, _meta(model_id, idea_id, "c1", "voided"), LEDGER)
    assert register_trial(space, _meta(model_id, idea_id, "c2", "complete"), LEDGER)


def test_complete_is_in_flight_because_it_awaits_adjudication() -> None:
    """'complete' means the RUN finished, not that the trial is settled -- that is exactly the
    state a duplicate dispatch races."""
    space, model_id, idea_id = _space()
    register_trial(space, _meta(model_id, idea_id, "c1", "complete"), LEDGER)
    with pytest.raises(RegistryValidationError):
        register_trial(space, _meta(model_id, idea_id, "c2", "running"), LEDGER)


def test_another_ideas_trial_is_unaffected() -> None:
    space, model_id, idea_id = _space()
    other = register_idea(space, {"model_id": model_id, "origin": "seeded",
                                  "axis": "architecture", "description": "d2"})
    register_trial(space, _meta(model_id, idea_id, "c1", "complete"), LEDGER)
    assert register_trial(space, _meta(model_id, other, "c2", "complete"), LEDGER)


def test_supersede_frees_a_dead_run_and_records_why() -> None:
    """The escape hatch. A run that died without resolving would otherwise wedge its idea forever."""
    space, model_id, idea_id = _space()
    tid = register_trial(space, _meta(model_id, idea_id, "c1", "complete"), LEDGER)
    supersede_trial(space, tid, reason="orphaned by a supervisor restart; process confirmed dead")
    assert space.get(tid).meta["status"] == "superseded"
    assert "orphaned" in space.get(tid).meta["superseded_reason"]
    assert register_trial(space, _meta(model_id, idea_id, "c2", "complete"), LEDGER)


def test_supersede_refuses_to_rewrite_a_settled_verdict() -> None:
    space, model_id, idea_id = _space()
    tid = register_trial(space, _meta(model_id, idea_id, "c1", "succeeded"), LEDGER)
    with pytest.raises(RegistryValidationError):
        supersede_trial(space, tid, reason="changed my mind")


def test_supersede_requires_a_stated_reason() -> None:
    """An abandoned trial with no reason is indistinguishable from one quietly dropped for losing,
    and the dead-ideas register depends on telling those apart."""
    space, model_id, idea_id = _space()
    tid = register_trial(space, _meta(model_id, idea_id, "c1", "complete"), LEDGER)
    with pytest.raises(RegistryValidationError):
        supersede_trial(space, tid, reason="   ")


# --- a DEAD worker's in-flight trial must not wedge its idea forever -------------------
# The idea claim has a lease; the trial has none. A worker that dies mid-trial leaves the
# trial `running` forever, while the stale claim returns the idea to the untried backlog --
# so _select_candidate keeps picking it and register_trial keeps refusing, uncaught, and
# every subsequent supervise-campaign died on the same idea.

_DISPATCH_MODEL_META = {
    "metric": "val_bpb", "direction": "minimize", "win_condition": "beats baseline by the rope",
    "baseline": "base", "baseline_runs": list(ROPE_ROWS),
    "baseline_throughput": 1200.0, "diff_size_limit": 800,
    "max_trials": 5, "max_discovered_ideas": 0,
}


def _dispatch_fixture(claim_age_s: float
                      ) -> tuple[RegistrySpace, str, str, str, dict[str, LedgerRow]]:
    """A model whose only idea is claimed ``claim_age_s`` ago under a 900s lease, with an
    in-flight trial left behind by that claim's worker."""
    import time as _time

    from knowledge.ml_registry.lifecycle import DEFAULT_IDEA_CLAIM_LEASE_TTL_S, claim_idea

    ledger = {
        "base": LedgerRow(value=1.0, throughput=1200.0, diff_lines=0),
        "dead": LedgerRow(value=1.0, throughput=1200.0, diff_lines=10),
        "fresh": LedgerRow(value=0.5, throughput=1200.0, diff_lines=10),
        **ROPE_ROWS,
    }
    space = RegistrySpace()
    model_id = register_model(space, dict(_DISPATCH_MODEL_META))
    idea_id = register_idea(space, {"model_id": model_id, "origin": "seeded",
                                    "axis": "architecture", "description": "d"})
    claim_idea(space, idea_id, "worker-1", ttl=DEFAULT_IDEA_CLAIM_LEASE_TTL_S,
               now=_time.time() - claim_age_s)
    stuck = register_trial(space, _meta(model_id, idea_id, "dead", "running"), frozenset(ledger))
    return space, model_id, idea_id, stuck, ledger


def test_a_stale_claims_in_flight_trial_is_auto_superseded_instead_of_wedging_the_campaign():
    from knowledge.ml_registry.lifecycle import STATUS_SUPERSEDED
    from knowledge.ml_registry.supervisor import dispatch_trial

    space, model_id, idea_id, stuck, ledger = _dispatch_fixture(claim_age_s=10_000)

    result = dispatch_trial(space, model_id, ledger, lambda s, m, i: {"commit": "fresh"})

    assert result["candidate"] == idea_id
    assert result["trial_id"] not in (None, stuck)
    assert space.get(stuck).meta["status"] == STATUS_SUPERSEDED
    assert "stale" in str(space.get(stuck).meta.get("superseded_reason", "")).lower()


def test_a_live_claim_never_reaches_the_auto_supersede_so_two_workers_cannot_race():
    """A slow worker is not a dead one: while its claim lease is LIVE the idea is not in the
    untried backlog at all, so no candidate is selected and its in-flight trial is left
    exactly as it is. That distinction -- lease live vs lease expired -- is what keeps the
    auto-supersede from ever cancelling a run that is genuinely still going."""
    from knowledge.ml_registry.supervisor import dispatch_trial

    space, model_id, idea_id, stuck, ledger = _dispatch_fixture(claim_age_s=1)

    result = dispatch_trial(space, model_id, ledger, lambda s, m, i: {"commit": "fresh"})

    assert result["candidate"] is None
    assert space.get(stuck).meta["status"] == "running"
