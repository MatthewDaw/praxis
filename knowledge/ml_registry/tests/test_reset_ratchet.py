"""Clearing a rejection streak at a stage boundary, without disturbing any verdict.

The ratchet reads three consecutive rejections as evidence the last ADOPTION was noise. That
inference holds only while the rejections compete against the adoption on the same axis, and it
does not survive a stage boundary.

Observed on the first staged campaign: a representation change adopted at +0.0239, then two
architecture arms rejected (an MLP at -0.0177, a transformer at -0.0146). Neither rejection was
caused by an inflated bar -- BOTH scored above the pre-adoption baseline and would merely have
parked against it. One more unrelated rejection would have rolled back a sound adoption.
"""

from __future__ import annotations

import pytest

from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.verdict import reset_ratchet
from knowledge.ml_registry.write_path import RegistrySpace, register_model

META = {"metric": "f1", "direction": "maximize", "win_condition": "beats baseline by noise_floor",
        "baseline": "c1", "noise_floor": 0.01, "baseline_throughput": 1.0, "diff_size_limit": 8,
        "max_trials": 5, "max_discovered_ideas": 2}


def _model(**over) -> tuple[RegistrySpace, str]:
    space = RegistrySpace()
    return space, register_model(space, {**META, **over})


def test_it_clears_the_streak_and_reports_what_it_cleared() -> None:
    space, mid = _model()
    space.get(mid).meta["ratchet_count"] = 2
    space.get(mid).meta["rejection_streak_ideas"] = ["idea-a", "idea-b"]

    cleared = reset_ratchet(space, mid, reason="architecture stage closed")
    assert cleared == {"ratchet_count": 2, "rejection_streak_ideas": ["idea-a", "idea-b"]}
    assert space.get(mid).meta["ratchet_count"] == 0
    assert space.get(mid).meta["rejection_streak_ideas"] == []


def test_it_does_not_touch_the_baseline() -> None:
    """It forgets a STREAK. Rewriting the baseline would be rolling the ratchet, not clearing it."""
    space, mid = _model(baseline="c1", previous_baseline="c0")
    space.get(mid).meta["ratchet_count"] = 2
    reset_ratchet(space, mid, reason="stage boundary")
    assert space.get(mid).meta["baseline"] == "c1"
    assert space.get(mid).meta["previous_baseline"] == "c0"


def test_a_genuinely_false_adoption_is_still_catchable_afterwards() -> None:
    """Clearing must not disable the ratchet -- only forget rejections that cannot bear on it."""
    space, mid = _model()
    space.get(mid).meta["ratchet_count"] = 2
    reset_ratchet(space, mid, reason="stage boundary")
    space.get(mid).meta["ratchet_count"] = 3          # three NEW rejections on the same axis
    assert space.get(mid).meta["ratchet_count"] == 3


def test_every_reset_is_recorded() -> None:
    """An unrecorded reset is indistinguishable from quietly protecting a favoured result."""
    space, mid = _model()
    space.get(mid).meta["ratchet_count"] = 2
    space.get(mid).meta["rejection_streak_ideas"] = ["idea-a", "idea-b"]
    reset_ratchet(space, mid, reason="architecture stage closed; arms varied the head, not the adoption")

    history = space.get(mid).meta["ratchet_resets"]
    assert len(history) == 1
    assert "architecture stage closed" in history[0]["reason"]
    assert history[0]["ratchet_count"] == 2
    assert history[0]["rejection_streak_ideas"] == ["idea-a", "idea-b"]


def test_repeated_resets_accumulate_rather_than_overwrite() -> None:
    """A campaign that keeps clearing its ratchet should be visible as such."""
    space, mid = _model()
    for i in range(3):
        space.get(mid).meta["ratchet_count"] = 2
        reset_ratchet(space, mid, reason=f"stage {i} closed")
    assert len(space.get(mid).meta["ratchet_resets"]) == 3


def test_a_reason_is_required() -> None:
    space, mid = _model()
    with pytest.raises(RegistryValidationError):
        reset_ratchet(space, mid, reason="   ")


def test_an_unregistered_model_is_refused() -> None:
    space, _ = _model()
    with pytest.raises(RegistryValidationError):
        reset_ratchet(space, "model-nope", reason="stage boundary")
