"""Pure min-of-axes verdict math (foundation for U4). Anti-loop invariants live here."""

from __future__ import annotations

import pytest

from agent_factory.rubric import Axis, Defect, Rubric, evaluate, rubric_from_dict


def _rubric(floor: int = 5, **thresholds) -> Rubric:
    axes = tuple(Axis(name=n, threshold=t) for n, t in thresholds.items())
    return Rubric(axes=axes, confidence_floor=floor)


def test_all_axes_clear_no_defects_passes():
    r = _rubric(a=0.7, b=0.6)
    v = evaluate(r, {"a": 0.9, "b": 0.8}, [])
    assert v.passed and v.min_axis == 0.8 and not v.defects


def test_min_of_axes_one_weak_axis_fails_despite_strong_others():
    r = _rubric(a=0.7, b=0.7)
    v = evaluate(r, {"a": 0.99, "b": 0.4}, [])
    assert not v.passed
    assert "b" in v.reason and v.min_axis == 0.4  # strong 'a' does not mask weak 'b'


def test_high_confidence_located_defect_fails():
    r = _rubric(floor=5, a=0.5)
    d = Defect(problem="sqli", remedy="parameterize", confidence=8, file="db.py", line=12)
    v = evaluate(r, {"a": 0.9}, [d])
    assert not v.passed and v.defects == (d,)


def test_defect_below_floor_is_dropped_and_does_not_fail():
    """The core anti-loop guard: a low-confidence would-be-failure never reopens the loop."""
    r = _rubric(floor=6, a=0.5)
    d = Defect(problem="maybe", remedy="?", confidence=3)
    v = evaluate(r, {"a": 0.9}, [d])
    assert v.passed and v.defects == ()


def test_dissatisfaction_without_located_defect_passes():
    """positive-evidence-of-defect-to-fail: axes clear + no credible defect => pass, even if the
    judge is vaguely unhappy (no located defect, no failing axis => no evidence to fail on)."""
    r = _rubric(floor=5, a=0.6)
    v = evaluate(r, {"a": 0.9}, [])
    assert v.passed


def test_per_axis_threshold_expresses_importance():
    """A stricter threshold on one axis fails a score a uniform bar would pass."""
    lenient = _rubric(a=0.6)
    strict = _rubric(a=0.9)
    assert evaluate(lenient, {"a": 0.8}, []).passed
    assert not evaluate(strict, {"a": 0.8}, []).passed


def test_missing_axis_score_is_a_fail_not_a_pass():
    r = _rubric(a=0.5, b=0.5)
    v = evaluate(r, {"a": 0.9}, [])  # 'b' never scored
    assert not v.passed and "b" in v.reason


def test_rubric_from_dict_roundtrip():
    r = rubric_from_dict({
        "confidence_floor": 7,
        "criterion": "x",
        "axes": [{"name": "a", "threshold": 0.5, "guidance": "g"}],
    })
    assert r.confidence_floor == 7 and r.axes[0].guidance == "g"


@pytest.mark.parametrize("bad", [
    {"axes": []},
    {"axes": [{"name": "", "threshold": 0.5}]},
    {"axes": [{"name": "a", "threshold": 2.0}]},
    {"axes": [{"name": "a"}]},
    {"axes": [{"name": "a", "threshold": 0.5}], "confidence_floor": 0},
])
def test_rubric_from_dict_rejects_malformed(bad):
    with pytest.raises(ValueError):
        rubric_from_dict(bad)
