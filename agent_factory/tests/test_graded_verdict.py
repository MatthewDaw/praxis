"""U4: the fresh-context judge wrapper (offline, stubbed Complete)."""

from __future__ import annotations

import json

import pytest

from agent_factory.graded_verdict import GradeError, grade, parse_judge_output
from agent_factory.rubric import Anchors, Axis, Rubric


def _rubric(floor: int = 5, **thresholds) -> Rubric:
    return Rubric(axes=tuple(Axis(n, t) for n, t in thresholds.items()), confidence_floor=floor,
                  criterion="c")


def _stub(payload: dict):
    return lambda _prompt: json.dumps(payload)


def test_all_axes_pass_no_defects():
    v = grade(_stub({"axis_scores": {"a": 0.9, "b": 0.8}, "defects": []}), _rubric(a=0.7, b=0.7), "diff")
    assert v.passed


def test_one_axis_below_threshold_fails():
    v = grade(_stub({"axis_scores": {"a": 0.9, "b": 0.4}, "defects": []}), _rubric(a=0.7, b=0.7), "diff")
    assert not v.passed and "b" in v.reason


def test_high_confidence_defect_fails():
    payload = {"axis_scores": {"a": 0.9}, "defects": [
        {"file": "x.py", "line": 3, "problem": "p", "remedy": "r", "confidence": 8}]}
    v = grade(_stub(payload), _rubric(floor=5, a=0.5), "diff")
    assert not v.passed and v.defects[0].file == "x.py"


def test_low_confidence_defect_dropped_passes():
    payload = {"axis_scores": {"a": 0.9}, "defects": [
        {"file": "x.py", "line": 3, "problem": "p", "remedy": "r", "confidence": 2}]}
    v = grade(_stub(payload), _rubric(floor=6, a=0.5), "diff")
    assert v.passed and v.defects == ()


def test_no_located_defect_passes_even_if_scores_are_just_okay():
    v = grade(_stub({"axis_scores": {"a": 0.6}, "defects": []}), _rubric(a=0.6), "diff")
    assert v.passed


def test_tolerates_json_fences_and_prose():
    text = "Here is my review:\n```json\n{\"axis_scores\": {\"a\": 0.9}, \"defects\": []}\n```\n"
    scores, defects = parse_judge_output(text)
    assert scores == {"a": 0.9} and defects == []


def test_malformed_output_raises_not_silent_pass():
    with pytest.raises(GradeError):
        grade(lambda _p: "the code looks fine to me", _rubric(a=0.5), "diff")


def test_missing_axis_scores_key_raises():
    with pytest.raises(GradeError, match="axis_scores"):
        parse_judge_output(json.dumps({"defects": []}))


def test_non_numeric_score_raises():
    with pytest.raises(GradeError):
        parse_judge_output(json.dumps({"axis_scores": {"a": "high"}, "defects": []}))


def test_prompt_names_axes_and_forbids_test_rejudging():
    from agent_factory.graded_verdict import build_judge_prompt
    p = build_judge_prompt(_rubric(a=0.7, sec=0.8), "DIFFBODY")
    assert "a (pass threshold 0.7)" in p and "sec (pass threshold 0.8)" in p
    assert "DIFFBODY" in p and "did NOT write this code" in p


# --------------------------------------------------------------------------- U2: anchor injection


def _anchored(good, slop) -> Rubric:
    return Rubric(axes=(Axis("minimalism", 0.8),), criterion="strict minimization",
                  anchors=Anchors(good=tuple(good), slop=tuple(slop)))


def test_anchors_injected_verbatim_under_calibration_heading():
    from agent_factory.graded_verdict import build_judge_prompt
    good = ["def add(a, b):\n    return a + b"]
    slop = ["def add(a, b):\n    result = a + b  # speculative unused local\n    return result"]
    p = build_judge_prompt(_anchored(good, slop), "DIFFBODY")
    assert "CALIBRATION" in p
    # Both snippets appear VERBATIM (the reproducibility claim: same anchors -> same framing).
    assert good[0] in p and slop[0] in p
    # Good is presented before slop.
    assert p.index(good[0]) < p.index(slop[0])


def test_no_anchor_prompt_is_byte_identical():
    """A rubric without anchors must produce the exact pre-anchor prompt (no CALIBRATION block)."""
    from agent_factory.graded_verdict import build_judge_prompt
    plain = _rubric(a=0.7)                       # anchors default None
    p = build_judge_prompt(plain, "DIFFBODY")
    assert "CALIBRATION" not in p
    # Identical to building the same rubric through the public path — no anchor residue.
    assert p == build_judge_prompt(Rubric(axes=plain.axes, confidence_floor=plain.confidence_floor,
                                          criterion=plain.criterion), "DIFFBODY")


def test_anchor_calibration_moves_the_verdict_end_to_end():
    """Behavioral (not just string) check: a calibrated rubric run through a judge that grades to
    the anchors FAILS a known-slop diff and PASSES a known-good diff — the reproducibility claim."""
    rubric = _anchored(good=["return a + b"], slop=["unused_local = a + b"])

    def scripted(prompt: str) -> str:
        # A faithful judge honoring the injected anchors: the slop snippet's shape scores low with a
        # located defect; the good snippet's shape scores high with none.
        diff = prompt.split("DIFF:\n", 1)[-1]
        if "unused_local" in diff:
            return json.dumps({"axis_scores": {"minimalism": 0.3}, "defects": [
                {"file": "m.py", "line": 2, "problem": "speculative unused local",
                 "remedy": "inline the expression", "confidence": 8}]})
        return json.dumps({"axis_scores": {"minimalism": 0.95}, "defects": []})

    assert not grade(scripted, rubric, "def f():\n    unused_local = a + b\n    return a + b").passed
    assert grade(scripted, rubric, "def f():\n    return a + b").passed
