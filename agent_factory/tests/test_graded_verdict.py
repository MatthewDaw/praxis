"""U4: the fresh-context judge wrapper (offline, stubbed Complete)."""

from __future__ import annotations

import json

import pytest

from agent_factory.graded_verdict import GradeError, grade, parse_judge_output
from agent_factory.rubric import Axis, Rubric


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
