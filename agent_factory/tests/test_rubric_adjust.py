"""U7: auto-adjust rubrics from review signal — proposals + human-gated apply."""

from __future__ import annotations

import textwrap

from agent_factory.rubric import Axis, Defect, Rubric
from agent_factory.rubric_adjust import (
    ADD_AXIS, CLARIFY, LOOSEN, STRENGTHEN,
    GradedObservation, aggregate, apply_proposals, propose,
)


def _rubric(**thresholds) -> Rubric:
    return Rubric(axes=tuple(Axis(n, t) for n, t in thresholds.items()), confidence_floor=5)


def _obs(converged=True, scores=None, defects=()):
    return GradedObservation("chk", converged, scores or {}, tuple(defects))


def _defect(problem, conf=8):
    return Defect(problem=problem, remedy="r", confidence=conf, file="f", line=1)


# ---- gating on signal volume ------------------------------------------------

def test_no_proposal_below_min_obs():
    sig = aggregate([_obs(converged=False, scores={"a": 0.2})] * 2)["chk"]
    assert propose(_rubric(a=0.7), sig, min_obs=3) == []


# ---- scenario: repeated non-convergence -> loosen + clarify, never strengthen

def test_non_convergence_loosens_binding_axis_and_clarifies():
    obs = [_obs(converged=False, scores={"a": 0.9, "b": 0.3}) for _ in range(4)]
    sig = aggregate(obs)["chk"]
    props = propose(_rubric(a=0.7, b=0.7), sig)
    kinds = {p.kind for p in props}
    assert kinds == {LOOSEN, CLARIFY}
    loosen = next(p for p in props if p.kind == LOOSEN)
    assert loosen.axis == "b" and loosen.to_value < loosen.from_value  # binding axis, lowered
    assert STRENGTHEN not in kinds  # bias: never tighten a non-converging check


# ---- scenario: recurring high-confidence defect (converging) -> strengthen ---

def test_recurring_defect_strengthens_lenient_axis():
    obs = [_obs(converged=True, scores={"a": 0.95}, defects=[_defect("missing null check")])
           for _ in range(4)]
    sig = aggregate(obs)["chk"]
    props = propose(_rubric(a=0.6), sig)
    assert len(props) == 1 and props[0].kind == STRENGTHEN
    assert props[0].axis == "a" and props[0].to_value > props[0].from_value


def test_recurring_defect_with_no_axis_proposes_add_axis():
    obs = [_obs(converged=True, scores={}, defects=[_defect("timeout unhandled")]) for _ in range(3)]
    sig = aggregate(obs)["chk"]
    props = propose(_rubric(a=0.6), sig)
    assert len(props) == 1 and props[0].kind == ADD_AXIS


def test_one_off_defect_is_not_systemic():
    obs = [_obs(converged=True, scores={"a": 0.95}, defects=[_defect("x")])] + \
          [_obs(converged=True, scores={"a": 0.95}) for _ in range(3)]
    sig = aggregate(obs)["chk"]
    assert propose(_rubric(a=0.6), sig, recur_min=2) == []  # theme on 1 ticket -> nothing


def test_low_confidence_defects_do_not_form_a_theme():
    obs = [_obs(converged=True, scores={"a": 0.95}, defects=[_defect("weak", conf=2)])
           for _ in range(4)]
    sig = aggregate(obs, floor=5)["chk"]
    assert propose(_rubric(a=0.6), sig) == []  # below floor -> dropped, not systemic


def test_well_calibrated_check_yields_nothing():
    obs = [_obs(converged=True, scores={"a": 0.8}) for _ in range(5)]
    sig = aggregate(obs)["chk"]
    assert propose(_rubric(a=0.7), sig) == []


# ---- apply: human-gated, in-flight-deferred ---------------------------------

_LIB = textwrap.dedent("""
    [[check]]
    check_id = "chk"
    kind = "graded"
    [[check.axes]]
    name = "a"
    threshold = 0.6
    [[check.axes]]
    name = "b"
    threshold = 0.7
""")


def _strengthen_a():
    return [next(p for p in propose(
        _rubric(a=0.6), aggregate([_obs(scores={"a": 0.95}, defects=[_defect("d")])
                                   for _ in range(3)])["chk"]))]


def test_apply_is_inert_without_confirm():
    props = _strengthen_a()
    res = apply_proposals(_LIB, props, in_flight=set(), confirm=False)
    assert res.text == _LIB and res.applied == () and res.skipped[0][1] == "unconfirmed"


def test_apply_edits_only_the_targeted_axis_threshold():
    props = _strengthen_a()
    res = apply_proposals(_LIB, props, in_flight=set(), confirm=True)
    assert res.applied and 'name = "a"\nthreshold = 0.65' in res.text
    assert 'name = "b"\nthreshold = 0.7' in res.text  # sibling axis untouched


def test_apply_defers_in_flight_check():
    props = _strengthen_a()
    res = apply_proposals(_LIB, props, in_flight={"chk"}, confirm=True)
    assert res.applied == () and "in-flight" in res.skipped[0][1]
    assert res.text == _LIB  # library unchanged while the ticket is building


def test_apply_skips_non_numeric_proposals():
    obs = [_obs(converged=False, scores={"a": 0.9}) for _ in range(4)]
    props = propose(_rubric(a=0.7), aggregate(obs)["chk"])  # yields LOOSEN + CLARIFY
    res = apply_proposals(_LIB, props, in_flight=set(), confirm=True)
    assert any(p.kind == LOOSEN for p in res.applied)
    assert any(pr.kind == CLARIFY for pr, _ in res.skipped)
