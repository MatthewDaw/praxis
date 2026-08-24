"""R3 acceptance: idea lifecycle -- adopt / park / reject, the idea claim lease, and
adoption reversal."""

from __future__ import annotations

import pytest

from knowledge.ml_registry.lifecycle import (
    STATUS_ADOPTED,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
    adopt_idea,
    claim_idea,
    flagged_trials,
    heartbeat_idea_claim,
    invalidate_adoption,
    is_retriable,
    park_idea,
    per_axis_yield,
    reject_idea,
    rejection_memory,
    supersede_adoption,
    untried_backlog,
)
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.write_path import RegistrySpace, register_idea, register_model, register_trial

MODEL_META = {
    "metric": "val_bpb",
    "direction": "minimize",
    "win_condition": "beats baseline by the rope",
    "baseline": "commit-abc123",
    "baseline_throughput": 1200,
    "diff_size_limit": 800,
}

LEDGER = frozenset({"deadbeef", "feedface", "c0ffee"})


def _idea_meta(model_id, *, description="try RoPE scaling"):
    return {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": description}


def _trial_meta(model_id, idea_id, *, commit="deadbeef", status="succeeded"):
    return {"model_id": model_id, "idea_id": idea_id, "commit": commit, "status": status}


def _model_and_idea(space):
    model_id = register_model(space, dict(MODEL_META))
    idea_id = register_idea(space, _idea_meta(model_id))
    return model_id, idea_id


def test_adopting_an_idea_from_a_succeeded_trial_records_a_succeeded_outcome():
    space = RegistrySpace()
    model_id, idea_id = _model_and_idea(space)
    trial_id = register_trial(space, _trial_meta(model_id, idea_id), LEDGER)

    adopt_idea(space, idea_id, trial_id)

    idea = space.get(idea_id)
    assert idea.meta["status"] == STATUS_ADOPTED
    assert idea.meta["outcome"] == "succeeded"
    assert idea.meta["adopted_trial_id"] == trial_id


def test_adopting_from_an_unsucceeded_trial_is_refused_naming_status():
    space = RegistrySpace()
    model_id, idea_id = _model_and_idea(space)
    trial_id = register_trial(space, _trial_meta(model_id, idea_id, status="running"), LEDGER)

    with pytest.raises(RegistryValidationError) as excinfo:
        adopt_idea(space, idea_id, trial_id)
    assert excinfo.value.field == "status"


def test_parking_an_idea_requires_a_non_empty_reactivation_trigger():
    space = RegistrySpace()
    _, idea_id = _model_and_idea(space)
    with pytest.raises(RegistryValidationError) as excinfo:
        park_idea(space, idea_id, "")
    assert excinfo.value.field == "reactivation_trigger"


def test_parked_idea_is_retriable_only_once_its_own_trigger_fires():
    space = RegistrySpace()
    _, idea_id = _model_and_idea(space)
    park_idea(space, idea_id, "new-dataset-released")
    idea = space.get(idea_id)

    assert idea.meta["reactivation_trigger"] == "new-dataset-released"
    assert not is_retriable(idea, fired_triggers={"unrelated-trigger"})
    assert is_retriable(idea, fired_triggers={"new-dataset-released"})


def test_rejected_idea_is_absent_from_backlog_but_present_with_reason_in_rejection_memory():
    space = RegistrySpace()
    _, idea_id = _model_and_idea(space)

    reject_idea(space, idea_id, "did not beat baseline")

    assert idea_id not in {i.id for i in untried_backlog(space)}
    memory = {i.id: reason for i, reason in rejection_memory(space)}
    assert memory[idea_id] == "did not beat baseline"


def test_rejecting_with_an_empty_reason_is_refused():
    space = RegistrySpace()
    _, idea_id = _model_and_idea(space)
    with pytest.raises(RegistryValidationError) as excinfo:
        reject_idea(space, idea_id, "")
    assert excinfo.value.field == "reason"


def test_derived_trials_of_a_rejected_idea_are_returned_flagged():
    space = RegistrySpace()
    model_id, idea_id = _model_and_idea(space)
    trial_id = register_trial(space, _trial_meta(model_id, idea_id, commit="feedface", status="running"), LEDGER)

    reject_idea(space, idea_id, "not promising")

    flagged = {t.id: t.meta.get("idea_rejected") for t in flagged_trials(space)}
    assert flagged[trial_id] is True


def test_a_stale_idea_claim_is_reclaimable_by_a_different_owner():
    space = RegistrySpace()
    _, idea_id = _model_and_idea(space)

    assert claim_idea(space, idea_id, "worker-a", ttl=10, now=1_000.0) is True
    # Still live for a different owner just before the ttl elapses.
    assert claim_idea(space, idea_id, "worker-b", ttl=10, now=1_005.0) is False
    # Past the ttl, the lease is stale and reclaimable by a different owner.
    assert claim_idea(space, idea_id, "worker-b", ttl=10, now=1_050.0) is True
    assert space.get(idea_id).meta["claim_owner"] == "worker-b"


def test_heartbeat_keeps_an_idea_claim_live_past_its_original_ttl():
    space = RegistrySpace()
    _, idea_id = _model_and_idea(space)

    claim_idea(space, idea_id, "worker-a", ttl=10, now=1_000.0)
    assert heartbeat_idea_claim(space, idea_id, "worker-a", now=1_008.0) is True
    # Reclaim attempt just after the ORIGINAL ttl would have expired -- but the heartbeat
    # refreshed it, so a different owner cannot take it yet.
    assert claim_idea(space, idea_id, "worker-b", ttl=10, now=1_012.0) is False


def test_invalidating_an_adoption_reverts_it_and_returns_ideas_rejected_during_its_tenure():
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))
    winner_id = register_idea(space, _idea_meta(model_id, description="the eventual winner"))
    loser_id = register_idea(space, _idea_meta(model_id, description="rejected mid-tenure"))
    bystander_id = register_idea(space, _idea_meta(model_id, description="rejected before any adoption"))

    # A rejection with no active adoption yet -- not part of any tenure.
    reject_idea(space, bystander_id, "unrelated rejection")

    trial_id = register_trial(space, _trial_meta(model_id, winner_id), LEDGER)
    adopt_idea(space, winner_id, trial_id)

    # Rejected WHILE winner_id is adopted -- part of its tenure.
    reject_idea(space, loser_id, "superseded by the winner")

    invalidate_adoption(space, winner_id, "regression found in production")

    winner = space.get(winner_id)
    assert winner.meta["status"] != STATUS_ADOPTED
    assert winner.meta["reversal_reason"] == "regression found in production"

    backlog_ids = {i.id for i in untried_backlog(space)}
    assert loser_id in backlog_ids  # returned to the backlog
    assert bystander_id not in backlog_ids  # untouched -- it predates the tenure
    assert {i.id for i, _ in rejection_memory(space)} == {bystander_id}


def test_superseding_an_adoption_demotes_it_without_touching_its_tenures_rejections():
    """The mirror image of invalidation: a better idea replaced this one, but it was a real
    bar while it stood, so the ideas rejected under it stay rejected."""
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))
    winner_id = register_idea(space, _idea_meta(model_id, description="the first winner"))
    loser_id = register_idea(space, _idea_meta(model_id, description="rejected mid-tenure"))

    trial_id = register_trial(space, _trial_meta(model_id, winner_id), LEDGER)
    adopt_idea(space, winner_id, trial_id)
    reject_idea(space, loser_id, "lost to the winner")

    supersede_adoption(space, winner_id, "superseded by a better idea")

    winner = space.get(winner_id)
    assert winner.meta["status"] == STATUS_SUPERSEDED
    assert winner.meta["reversal_reason"] == "superseded by a better idea"
    assert "outcome" not in winner.meta and "adopted_trial_id" not in winner.meta

    loser = space.get(loser_id)
    assert loser.meta["status"] == STATUS_REJECTED
    assert loser.meta["rejection_reason"] == "lost to the winner"
    assert loser.meta["rejected_under_adoption"] == winner_id
    assert {i.id for i, _ in rejection_memory(space)} == {loser_id}


def test_a_superseded_idea_never_re_enters_the_untried_backlog():
    space = RegistrySpace()
    model_id, idea_id = _model_and_idea(space)
    trial_id = register_trial(space, _trial_meta(model_id, idea_id), LEDGER)
    adopt_idea(space, idea_id, trial_id)

    supersede_adoption(space, idea_id, "a better idea won")

    assert idea_id not in {i.id for i in untried_backlog(space)}


@pytest.mark.parametrize("reason,field", [("", "reason"), ("a real reason", "idea_id")])
def test_superseding_refuses_an_empty_reason_or_a_non_adopted_idea(reason, field):
    space = RegistrySpace()
    model_id, idea_id = _model_and_idea(space)
    if field == "reason":
        trial_id = register_trial(space, _trial_meta(model_id, idea_id), LEDGER)
        adopt_idea(space, idea_id, trial_id)
    with pytest.raises(RegistryValidationError) as excinfo:
        supersede_adoption(space, idea_id, reason)
    assert excinfo.value.field == field


def test_invalidating_an_adoption_that_is_not_currently_adopted_is_refused():
    space = RegistrySpace()
    _, idea_id = _model_and_idea(space)
    with pytest.raises(RegistryValidationError) as excinfo:
        invalidate_adoption(space, idea_id, "some reason")
    assert excinfo.value.field == "idea_id"


def test_per_axis_yield_counts_attempts_and_adoptions_per_axis_and_per_origin():
    space = RegistrySpace()
    model_id = register_model(space, dict(MODEL_META))

    # architecture/seeded: one attempted (trial registered) and adopted.
    arch_winner = register_idea(space, _idea_meta(model_id, description="arch winner"))
    trial_id = register_trial(space, _trial_meta(model_id, arch_winner, commit="deadbeef"), LEDGER)
    adopt_idea(space, arch_winner, trial_id)

    # architecture/seeded: a second idea, attempted but not adopted.
    arch_tried = register_idea(space, _idea_meta(model_id, description="arch tried"))
    register_trial(space, _trial_meta(model_id, arch_tried, commit="feedface", status="running"), LEDGER)

    # architecture/discovered: never attempted (still untried).
    arch_untried = dict(_idea_meta(model_id, description="arch untried"))
    arch_untried["origin"] = "discovered"
    register_idea(space, arch_untried)

    # data/seeded: never attempted.
    data_untried = dict(_idea_meta(model_id, description="data untried"))
    data_untried["axis"] = "data"
    register_idea(space, data_untried)

    report = per_axis_yield(space)

    assert report["architecture"]["seeded"] == {"attempts": 2, "adoptions": 1}
    assert report["architecture"]["discovered"] == {"attempts": 0, "adoptions": 0}
    assert report["data"]["seeded"] == {"attempts": 0, "adoptions": 0}
    assert "data" not in report or "discovered" not in report["data"]


def test_per_axis_yield_scopes_to_a_single_model_id():
    space = RegistrySpace()
    model_a = register_model(space, dict(MODEL_META))
    model_b = register_model(space, dict(MODEL_META))
    register_idea(space, _idea_meta(model_a, description="a's untried idea"))
    b_idea = register_idea(space, _idea_meta(model_b, description="b's attempted idea"))
    register_trial(space, _trial_meta(model_b, b_idea, commit="deadbeef", status="running"), LEDGER)

    report = per_axis_yield(space, model_id=model_a)

    # model_b's attempt must not leak into model_a's scoped report.
    assert report["architecture"]["seeded"] == {"attempts": 0, "adoptions": 0}


def test_invalidating_an_adoption_with_an_empty_reason_is_refused():
    space = RegistrySpace()
    model_id, idea_id = _model_and_idea(space)
    trial_id = register_trial(space, _trial_meta(model_id, idea_id), LEDGER)
    adopt_idea(space, idea_id, trial_id)
    with pytest.raises(RegistryValidationError) as excinfo:
        invalidate_adoption(space, idea_id, "")
    assert excinfo.value.field == "reason"
