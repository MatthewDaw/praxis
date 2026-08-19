"""A campaign is not finished because its queue emptied.

The first real campaign ran a partial architecture search and stopped. Augmentation, training,
tuning and capacity were never reached, no final train-to-convergence existed as a concept, and
every stage transition needed a human. Nothing errored -- each invocation exited 0 having done
what it was asked, and what it was asked was one stage's worth of arms.
"""

from __future__ import annotations

import pytest

from knowledge.ml_registry.completeness import CONVERGENCE_FIELD, campaign_completeness
from knowledge.ml_registry.write_path import (RegistrySpace, register_idea, register_model,
                                              register_trial)

STAGES = ("representation", "architecture", "tuning")
META = {"metric": "f1", "direction": "maximize", "win_condition": "beats baseline by noise_floor",
        "baseline": "c1", "noise_floor": 0.0115, "baseline_throughput": 3.38,
        "diff_size_limit": 8, "max_trials": 40, "max_discovered_ideas": 2}


def _space():
    space = RegistrySpace()
    return space, register_model(space, dict(META))


def _idea(space, mid, tag, axis, status=None):
    iid = register_idea(space, {"model_id": mid, "origin": "seeded", "axis": axis,
                                "description": tag, "id": tag})
    if status:
        space.get(iid).meta["status"] = status
    return iid


def _trial(space, mid, iid, commit, status="succeeded"):
    tid = register_trial(space, {"model_id": mid, "idea_id": iid, "commit": commit,
                                 "status": "complete", "throughput": 3.4, "diff_lines": 1},
                         frozenset({commit}))
    space.get(tid).meta["status"] = status
    return tid


def _full(space, mid, stage, n=3, offset=0):
    for i in range(n):
        iid = _idea(space, mid, f"{stage[:2]}{offset+i}", stage, status="rejected")
        _trial(space, mid, iid, f"c{stage[:2]}{offset+i}")


def test_an_empty_stage_blocks_rather_than_closing_silently() -> None:
    """The nastiest case: a stage with no arms is trivially 'all answered', so it closes instantly
    and the campaign sails past a question nobody ever asked. `tuning` and `capacity` were both
    empty on the first campaign and neither was mentioned anywhere."""
    space, mid = _space()
    for s in ("representation", "architecture"):
        _full(space, mid, s)
    space.get(mid).meta[CONVERGENCE_FIELD] = "c-final"

    out = campaign_completeness(space, mid, STAGES)
    assert not out["done"]
    blocked = [b for b in out["blocking"] if b["kind"] == "stage_never_authored"]
    assert [b["stage"] for b in blocked] == ["tuning"]


def test_a_campaign_with_no_convergence_run_is_not_finished() -> None:
    """Every arm is a short CV probe tuned to discriminate between candidates, not a trained
    model. Selecting a winner and never training it is half a job."""
    space, mid = _space()
    for s in STAGES:
        _full(space, mid, s)

    out = campaign_completeness(space, mid, STAGES)
    assert not out["done"]
    assert any(b["kind"] == "no_convergence_run" for b in out["blocking"])


def test_all_stages_populated_closed_and_converged_is_done() -> None:
    space, mid = _space()
    for s in STAGES:
        _full(space, mid, s)
    space.get(mid).meta[CONVERGENCE_FIELD] = "c-final"

    out = campaign_completeness(space, mid, STAGES)
    assert out["done"], out["blocking"]


def test_an_open_stage_blocks() -> None:
    space, mid = _space()
    _full(space, mid, "representation")
    _full(space, mid, "architecture")
    _idea(space, mid, "tu0", "tuning")                       # untried
    space.get(mid).meta[CONVERGENCE_FIELD] = "c-final"

    out = campaign_completeness(space, mid, STAGES)
    assert any(b["kind"] == "stage_open" and b["stage"] == "tuning" for b in out["blocking"])


def test_a_voided_arm_blocks_because_it_is_unmeasured() -> None:
    """Voided means the run was unfair, so the question is still open -- not answered."""
    space, mid = _space()
    for s in STAGES:
        _full(space, mid, s)
    iid = _idea(space, mid, "extra", "architecture", status="voided")
    _trial(space, mid, iid, "cx", status="voided")
    space.get(mid).meta[CONVERGENCE_FIELD] = "c-final"

    out = campaign_completeness(space, mid, STAGES)
    assert any(b["kind"] == "awaiting_rerun" for b in out["blocking"])


def test_a_thin_stage_blocks_even_though_it_closed() -> None:
    space, mid = _space()
    _full(space, mid, "representation")
    _full(space, mid, "architecture", n=1)                   # closed on ONE measured arm
    _full(space, mid, "tuning")
    space.get(mid).meta[CONVERGENCE_FIELD] = "c-final"

    out = campaign_completeness(space, mid, STAGES)
    assert any(b["kind"] == "stage_thin" and b["stage"] == "architecture"
               for b in out["blocking"])


def test_an_arm_whose_dependency_is_not_an_idea_does_not_hold_its_stage_open() -> None:
    """S06-style prose depends_on must not block campaign-complete after the rest of the stage ran."""
    space, mid = _space()
    _full(space, mid, "representation")
    _full(space, mid, "architecture")
    _full(space, mid, "tuning")
    register_idea(space, {"model_id": mid, "origin": "seeded", "axis": "tuning",
                          "description": "needs player tracks", "id": "S06",
                          "depends_on": ["player tracks on the same frames"]})
    space.get(mid).meta[CONVERGENCE_FIELD] = "c-final"

    out = campaign_completeness(space, mid, STAGES)
    assert out["done"], out["blocking"]


def test_convergence_can_be_waived_explicitly() -> None:
    """A campaign that only ever meant to SELECT, never to ship, may say so -- deliberately."""
    space, mid = _space()
    for s in STAGES:
        _full(space, mid, s)
    out = campaign_completeness(space, mid, STAGES, require_convergence=False)
    assert out["done"], out["blocking"]


def test_an_unregistered_model_is_refused() -> None:
    space, _ = _space()
    with pytest.raises(KeyError):
        campaign_completeness(space, "model-nope", STAGES)


def test_unreachable_arms_do_not_block_completion() -> None:
    """A composition arm gated on an idea that PARKED can never become eligible, because
    depends_on requires ADOPTION. Counting it as open makes `done` unreachable no matter what
    else is authored.

    This regressed silently: the union with unreachable() was present, but `items` was built
    WITHOUT depends_on, so unreachable() saw no dependencies and always returned nothing.
    Measured: a campaign exhausted its runnable backlog with four dead composition arms, one per
    stage, and campaign-complete reported four blockers no further work could ever clear.
    """
    space, mid = _space()
    for s in STAGES:
        _full(space, mid, s)
    # a composition arm whose dependency parked -- permanently ineligible
    dep = _idea(space, mid, "R01", "representation", status="parked")
    register_idea(space, {"model_id": mid, "origin": "seeded", "axis": "representation",
                          "description": "compose", "id": "R07", "depends_on": ["R01"]})
    space.get(mid).meta[CONVERGENCE_FIELD] = "c-final"

    out = campaign_completeness(space, mid, STAGES)
    blocking = {b["kind"] for b in out["blocking"]}
    assert "stage_open" not in blocking, out["blocking"]
