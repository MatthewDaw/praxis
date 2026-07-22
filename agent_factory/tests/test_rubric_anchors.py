"""U1: literal copy-pasted good/slop code ANCHORS on the rubric type.

Anchors are verbatim text (no scoring, no versioning) that pin the judge's taste for a subjective
check. This locks that the rubric type carries them, ``rubric_from_dict`` parses/validates them, and
``rubric_to_dict`` round-trips them — while a rubric WITHOUT anchors stays byte-identical to every
existing rubric (absent -> None, no ``anchors`` key on serialization).
"""

from __future__ import annotations

import pytest

from agent_factory.rubric import Anchors, rubric_from_dict, rubric_to_dict

_AXES = [{"name": "a", "threshold": 0.5, "guidance": "g"}]


def test_parses_good_and_slop_lists():
    r = rubric_from_dict({
        "axes": _AXES,
        "anchors": {"good": ["min good 1", "min good 2"], "slop": ["dead-code slop"]},
    })
    assert isinstance(r.anchors, Anchors)
    assert r.anchors.good == ("min good 1", "min good 2")
    assert r.anchors.slop == ("dead-code slop",)


def test_absent_anchors_is_none_and_not_serialized():
    r = rubric_from_dict({"axes": _AXES})
    assert r.anchors is None
    # A no-anchor rubric serializes WITHOUT an "anchors" key — byte-compatible with pre-anchor rubrics.
    assert "anchors" not in rubric_to_dict(r)


def test_roundtrips_through_to_dict():
    src = {
        "confidence_floor": 8,
        "criterion": "strict minimization",
        "judge_prompt": "be strict",
        "axes": [{"name": "minimalism", "threshold": 0.8, "guidance": "no dead code"}],
        "anchors": {"good": ["g1"], "slop": ["s1", "s2"]},
    }
    r = rubric_from_dict(src)
    d = rubric_to_dict(r)
    assert d["anchors"] == {"good": ["g1"], "slop": ["s1", "s2"]}
    # Re-parsing the serialized form yields an equivalent rubric (anchors preserved verbatim).
    assert rubric_from_dict(d).anchors == r.anchors


def test_partial_anchors_default_missing_side_to_empty():
    r = rubric_from_dict({"axes": _AXES, "anchors": {"good": ["only good"]}})
    assert r.anchors.good == ("only good",) and r.anchors.slop == ()


@pytest.mark.parametrize("bad", [
    {"axes": _AXES, "anchors": "not-a-dict"},
    {"axes": _AXES, "anchors": {"good": "not-a-list"}},
    {"axes": _AXES, "anchors": {"slop": 3}},
])
def test_malformed_anchors_rejected(bad):
    with pytest.raises(ValueError):
        rubric_from_dict(bad)
