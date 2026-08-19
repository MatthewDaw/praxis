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
