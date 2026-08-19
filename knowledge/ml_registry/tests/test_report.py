"""`campaign-status`: the answer to "how is it going", in one read-only command.

Written because that question was asked repeatedly during the first real campaign and every
answer meant hand-rolling a script against the space file over ssh. A status that is laborious
to obtain is a status nobody checks -- and the two worst failures of that campaign (a silently
wedged stage, and two runs racing on one idea) were both things a routine glance would have
caught immediately.
"""

from __future__ import annotations

import pytest

from knowledge.ml_registry.report import campaign_status, format_status
from knowledge.ml_registry.write_path import (RegistrySpace, register_idea, register_model,
                                              register_trial)

META = {"metric": "f1", "direction": "maximize", "win_condition": "beats baseline by noise_floor",
        "baseline": "c1", "noise_floor": 0.0115, "baseline_throughput": 3.38,
        "diff_size_limit": 8, "max_trials": 9, "max_discovered_ideas": 2}


def _space() -> tuple[RegistrySpace, str]:
    space = RegistrySpace()
    return space, register_model(space, dict(META))


def _idea(space, mid, tag, axis="architecture", **meta):
    return register_idea(space, {"model_id": mid, "origin": "seeded", "axis": axis,
                                 "description": tag, "id": tag, **meta})


def test_it_groups_ideas_by_verdict() -> None:
    space, mid = _space()
    for tag, st in (("R01", "parked"), ("R03", "adopted"), ("M01", "rejected")):
        iid = _idea(space, mid, tag)
        space.get(iid).meta["status"] = st
    _idea(space, mid, "M06")                       # untried

    st = campaign_status(space, mid)
    assert st["ideas_total"] == 4
    assert st["ideas_by_status"]["adopted"] == ["R03"]
    assert st["ideas_by_status"]["untried"] == ["M06"]


def test_it_surfaces_an_in_flight_trial() -> None:
    """The condition that is otherwise invisible: if no process is running, that run died and its
    idea is wedged until superseded."""
    space, mid = _space()
    iid = _idea(space, mid, "M06")
    register_trial(space, {"model_id": mid, "idea_id": iid, "commit": "c1", "status": "complete",
                           "throughput": 3.4, "diff_lines": 1}, frozenset({"c1"}))

    st = campaign_status(space, mid)
    assert len(st["trials_in_flight"]) == 1
    assert st["trials_in_flight"][0]["commit"] == "c1"
    assert "IN FLIGHT" in format_status(st)


def test_a_resolved_trial_is_not_in_flight() -> None:
    space, mid = _space()
    iid = _idea(space, mid, "M06")
    tid = register_trial(space, {"model_id": mid, "idea_id": iid, "commit": "c1",
                                 "status": "complete", "throughput": 3.4, "diff_lines": 1},
                         frozenset({"c1"}))
    space.get(tid).meta["status"] = "succeeded"
    assert campaign_status(space, mid)["trials_in_flight"] == []


def test_it_shows_the_ratchet_before_it_fires() -> None:
    """2/3 is the number worth seeing; at 3 the last adoption is already rolled back."""
    space, mid = _space()
    space.get(mid).meta["ratchet_count"] = 2
    space.get(mid).meta["rejection_streak_ideas"] = ["idea-a", "idea-b"]
    rendered = format_status(campaign_status(space, mid))
    assert "RATCHET" in rendered and "2/3" in rendered


def test_no_ratchet_line_when_the_streak_is_clean() -> None:
    space, mid = _space()
    assert "RATCHET" not in format_status(campaign_status(space, mid))


def test_an_unregistered_model_is_refused() -> None:
    space, _ = _space()
    with pytest.raises(KeyError):
        campaign_status(space, "model-nope")


def test_it_surfaces_a_disabled_speed_void_gate() -> None:
    """void_throughput_fraction=0 is a load-bearing campaign setting after #30; status
    that omits it makes a CV campaign look like it still has a 5% speed gate."""
    space, mid = _space()
    space.get(mid).meta["void_throughput_fraction"] = 0
    st = campaign_status(space, mid)
    assert st["void_throughput_fraction"] == 0
    assert "speed_void=0" in format_status(st)


def _voided(space, mid, iid, commit, reason):
    tid = register_trial(space, {"model_id": mid, "idea_id": iid, "commit": commit,
                                 "status": "complete", "throughput": 3.4, "diff_lines": 1},
                         frozenset({commit}))
    space.get(tid).meta["status"] = "voided"
    space.get(tid).meta["void_reason"] = reason
    return tid


def test_repeated_unfair_voids_diagnose_the_budget_not_the_arms() -> None:
    """One truncation is bad luck. Two says the wall clock will truncate them again, so an
    autonomous loop that merely re-runs will void forever and close having explained nothing."""
    from knowledge.ml_registry.report import diagnose

    space, mid = _space()
    for tag, c in (("M06", "c1"), ("M08", "c2")):
        _voided(space, mid, _idea(space, mid, tag), c, "ledger status 'budget_exhausted' is not a fair run")

    kinds = {d["kind"] for d in diagnose(space, mid)}
    assert "budget_too_small" in kinds
    detail = next(d for d in diagnose(space, mid) if d["kind"] == "budget_too_small")["detail"]
    assert "RE-RUNNING WILL NOT HELP" in detail


def test_repeated_throughput_voids_diagnose_the_gate() -> None:
    """A structurally slower arm can never pass a speed gate, so the gate is rejecting on cost."""
    from knowledge.ml_registry.report import diagnose

    space, mid = _space()
    for tag, c in (("R03", "c1"), ("M06", "c2")):
        _voided(space, mid, _idea(space, mid, tag), c, "throughput 3.17 is more than 5% below ...")

    assert "void_gate_too_tight" in {d["kind"] for d in diagnose(space, mid)}


def test_one_void_is_not_yet_a_diagnosis() -> None:
    from knowledge.ml_registry.report import diagnose

    space, mid = _space()
    _voided(space, mid, _idea(space, mid, "M06"), "c1", "ledger status 'x' is not a fair run")
    assert "budget_too_small" not in {d["kind"] for d in diagnose(space, mid)}


def test_ideas_whose_latest_trial_voided_are_named_as_awaiting_rerun() -> None:
    """A voided arm is UNMEASURED. Nothing else says so, and treating it as answered records
    nothing at all -- strictly worse than a rejection."""
    from knowledge.ml_registry.report import diagnose

    space, mid = _space()
    _voided(space, mid, _idea(space, mid, "M06"), "c1", "ledger status 'x' is not a fair run")
    assert "awaiting_rerun" in {d["kind"] for d in diagnose(space, mid)}


def test_a_later_resolved_trial_clears_awaiting_rerun() -> None:
    """Re-run and resolved is no longer awaiting anything."""
    from knowledge.ml_registry.report import diagnose

    space, mid = _space()
    iid = _idea(space, mid, "M06")
    _voided(space, mid, iid, "c1", "ledger status 'x' is not a fair run")
    tid = register_trial(space, {"model_id": mid, "idea_id": iid, "commit": "c2",
                                 "status": "complete", "throughput": 3.4, "diff_lines": 1},
                         frozenset({"c2"}))
    space.get(tid).meta["status"] = "succeeded"
    assert "awaiting_rerun" not in {d["kind"] for d in diagnose(space, mid)}


def test_diagnoses_appear_in_the_rendered_status() -> None:
    from knowledge.ml_registry.report import campaign_status, format_status

    space, mid = _space()
    for tag, c in (("M06", "c1"), ("M08", "c2")):
        _voided(space, mid, _idea(space, mid, tag), c, "ledger status 'budget_exhausted' is not a fair run")
    assert "BLOCKING: budget_too_small" in format_status(campaign_status(space, mid))
