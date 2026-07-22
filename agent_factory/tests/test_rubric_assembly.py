"""U5 — deterministic per-ticket rubric assembly over the candidate pool.

``assemble`` tiers a ticket's graded ``pool_candidates`` into:
  * up to ``budget`` PROMOTED individual gating graded validations (highest severity first,
    stable tie-break), each keeping its own rubric; and
  * ONE advisory-aggregate graded validation folding the rest — its rubric unions the folded
    candidates' axes at a soft-floor threshold, so the companion's min-of-axes verdict makes it
    min-of-candidates (a single egregious folded axis fails; a mediocre one does not).
"""

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from agent_factory import rubric_assembly as ra  # noqa: E402
from agent_factory.rubric import evaluate, rubric_from_dict  # noqa: E402


def cand(cid, severity, axes, candidate=True):
    """A graded pool candidate. ``axes`` is a list of (name, threshold)."""
    return {
        "id": cid, "category": "check", "scope": "validation",
        "meta": {
            "check_id": cid, "kind": "graded", "candidate": candidate, "severity": severity,
            "rubric": {"axes": [{"name": n, "threshold": t} for n, t in axes]},
        },
    }


def _ids(validations):
    return [v["validation_id"] for v in validations]


def _aggregate(validations):
    return next((v for v in validations if v["validation_id"] == ra.AGGREGATE_ID), None)


# --------------------------------------------------------------------------- promotion + tiering

def test_promotes_top_by_severity_under_budget():
    cands = [cand("hi-a", 3, [("a", 0.8)]), cand("hi-b", 3, [("b", 0.8)])] + \
            [cand(f"lo-{i}", 1, [("c", 0.7)]) for i in range(5)]
    out = ra.assemble(cands, budget=3)
    promoted = [v for v in out if v["validation_id"] != ra.AGGREGATE_ID]
    assert len(promoted) == 3                                  # exactly budget promoted
    promoted_sources = {v["source_check_id"] for v in promoted}
    assert {"hi-a", "hi-b"} <= promoted_sources                # both high-severity promoted
    agg = _aggregate(out)
    assert agg is not None                                     # the remaining 4 folded


def test_promoted_validation_keeps_its_own_rubric():
    c = cand("keep", 3, [("craft", 0.9)])
    out = ra.assemble([c], budget=3)
    promoted = out[0]
    assert promoted["kind"] == "graded"
    assert promoted["source_check_id"] == "keep"
    assert promoted["rubric"] == c["meta"]["rubric"]           # frozen provenance is the candidate


def test_exactly_one_aggregate_regardless_of_count():
    cands = [cand(f"c{i}", 1, [("x", 0.7)]) for i in range(20)]
    out = ra.assemble(cands, budget=2)
    aggregates = [v for v in out if v["validation_id"] == ra.AGGREGATE_ID]
    assert len(aggregates) == 1                                # O(1) aggregate invariant


def test_all_promoted_no_aggregate_when_under_budget():
    cands = [cand("c1", 2, [("x", 0.7)]), cand("c2", 1, [("y", 0.7)])]
    out = ra.assemble(cands, budget=5)
    assert _aggregate(out) is None                            # nothing folded -> no aggregate
    assert len(out) == 2


# --------------------------------------------------------------------------- min-of-candidates

def test_aggregate_is_min_of_candidates_not_average():
    # Two folded candidates; one scores egregiously low -> aggregate must FAIL (min, not average).
    folded = [cand("c1", 1, [("a", 0.8)]), cand("c2", 1, [("b", 0.8)])]
    out = ra.assemble(folded, budget=0, soft_floor=0.2)
    rub = rubric_from_dict(_aggregate(out)["rubric"])
    names = rub.axis_names()
    good = {n: 0.9 for n in names}
    assert evaluate(rub, good, []).passed is True             # both clear the soft floor
    bad = dict(good)
    bad[[n for n in names if n.startswith("c2")][0]] = 0.05
    assert evaluate(rub, bad, []).passed is False             # one egregious axis fails the whole


def test_soft_floor_gates_egregious_not_mediocre():
    folded = [cand("c1", 1, [("a", 0.9)])]
    out = ra.assemble(folded, budget=0, soft_floor=0.2)
    rub = rubric_from_dict(_aggregate(out)["rubric"])
    name = rub.axis_names()[0]
    assert evaluate(rub, {name: 0.5}, []).passed is True      # mediocre (>= floor) does not gate
    assert evaluate(rub, {name: 0.1}, []).passed is False     # egregious (< floor) gates


def test_informational_soft_floor_zero_never_gates_on_score():
    folded = [cand("c1", 1, [("a", 0.9)])]
    out = ra.assemble(folded, budget=0, soft_floor=0.0)
    rub = rubric_from_dict(_aggregate(out)["rubric"])
    name = rub.axis_names()[0]
    assert evaluate(rub, {name: 0.0}, []).passed is True      # threshold 0 -> purely informational


# --------------------------------------------------------------------------- determinism + overflow

def test_deterministic_across_calls():
    cands = [cand("b", 2, [("x", 0.7)]), cand("a", 2, [("x", 0.7)]), cand("c", 1, [("y", 0.7)])]
    first = ra.assemble(cands, budget=1)
    second = ra.assemble(cands, budget=1)
    assert _ids(first) == _ids(second)                        # same order + same promoted set
    promoted = [v for v in first if v["validation_id"] != ra.AGGREGATE_ID][0]
    assert promoted["source_check_id"] == "a"                 # sev tie broken by check_id asc


def test_budget_overflow_folds_and_is_recorded():
    cands = [cand(f"hi-{i}", 3, [(f"ax{i}", 0.8)]) for i in range(4)]
    out = ra.assemble(cands, budget=2)
    promoted = [v for v in out if v["validation_id"] != ra.AGGREGATE_ID]
    assert len(promoted) == 2                                 # budget cap holds
    agg_axes = {a["name"] for a in _aggregate(out)["rubric"]["axes"]}
    # the two overflow high-severity candidates are RECORDED as aggregate axes, not dropped
    assert any(n.startswith("hi-2:") for n in agg_axes)
    assert any(n.startswith("hi-3:") for n in agg_axes)


# --------------------------------------------------------------------------- edges

def test_zero_candidates_returns_empty():
    assert ra.assemble([], budget=3) == []


def test_non_graded_candidates_ignored():
    binary = {"id": "bin", "category": "check", "meta": {"check_id": "bin", "kind": "binary",
                                                         "candidate": True, "run": "pytest"}}
    assert ra.assemble([binary], budget=3) == []             # no rubric -> not tiered here
