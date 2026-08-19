"""Reopening an idea whose verdict was never fairly earned.

Not a way to relitigate a verdict you dislike -- for the narrow case where the run that produced
it should never have been adjudicated. Observed on the first campaign to run an expensive arm: a
graph model was starved by a 30-minute budget, its later folds scored UNTRAINED, and the mean over
trained and untrained folds was adjudicated as a -0.2766 rejection of the entire graph family.
Voiding the trial did not help -- the IDEA stayed rejected, so the arm could never be re-run.
"""

from __future__ import annotations

import pytest

from knowledge.ml_registry.lifecycle import reject_idea, reopen_idea
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.write_path import RegistrySpace, register_idea, register_model

META = {"metric": "f1", "direction": "maximize", "win_condition": "beats baseline by noise_floor",
        "baseline": "c1", "noise_floor": 0.0115, "baseline_throughput": 3.38,
        "diff_size_limit": 8, "max_trials": 9, "max_discovered_ideas": 2}


def _rejected() -> tuple[RegistrySpace, str]:
    space = RegistrySpace()
    mid = register_model(space, dict(META))
    iid = register_idea(space, {"model_id": mid, "origin": "seeded", "axis": "architecture",
                                "description": "a graph head", "id": "M06"})
    reject_idea(space, iid, reason="scored -0.2766")
    return space, iid


def test_it_returns_the_idea_to_untried() -> None:
    space, iid = _rejected()
    out = reopen_idea(space, iid, reason="run was truncated; later folds were untrained")
    assert space.get(iid).meta["status"] == "untried"
    assert out["previous_status"] == "rejected"


def test_the_prior_verdict_is_preserved_not_erased() -> None:
    """Reopening must be distinguishable from quietly deleting an inconvenient result."""
    space, iid = _rejected()
    reopen_idea(space, iid, reason="budget_exhausted; not a fair arm")
    history = space.get(iid).meta["reopened_from"]
    assert len(history) == 1
    assert history[0]["status"] == "rejected"
    assert history[0]["rejection_reason"] == "scored -0.2766"
    assert "budget_exhausted" in history[0]["reason"]


def test_stale_rejection_fields_do_not_survive() -> None:
    """An untried idea carrying a rejection_reason would read as rejected to anything scanning meta."""
    space, iid = _rejected()
    reopen_idea(space, iid, reason="unfair run")
    assert "rejection_reason" not in space.get(iid).meta


def test_repeated_reopens_accumulate() -> None:
    """An idea reopened again and again should be visible as such."""
    space, iid = _rejected()
    for i in range(2):
        reopen_idea(space, iid, reason=f"unfair run {i}")
        reject_idea(space, iid, reason="lost again")
    reopen_idea(space, iid, reason="unfair run 2")
    assert len(space.get(iid).meta["reopened_from"]) == 3


def test_an_untried_idea_has_nothing_to_reopen() -> None:
    space = RegistrySpace()
    mid = register_model(space, dict(META))
    iid = register_idea(space, {"model_id": mid, "origin": "seeded", "axis": "architecture",
                                "description": "d"})
    with pytest.raises(RegistryValidationError):
        reopen_idea(space, iid, reason="whatever")


def test_a_reason_is_required() -> None:
    space, iid = _rejected()
    with pytest.raises(RegistryValidationError):
        reopen_idea(space, iid, reason="  ")
