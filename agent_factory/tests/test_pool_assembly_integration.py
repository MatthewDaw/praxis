"""Integration — the assembler's output actually pins through the REAL companion engine path.

Proves the pattern end-to-end at the pin layer: ``rubric_assembly.assemble`` -> ``pin_validations``
-> ``_norm_validation`` yields graded pinned validations carrying their frozen rubric (which
``verify_graded_check`` then grades). No new pin/verify code — the assembler rides the companion's
existing ``kind="graded"`` validation path.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
_SRC = str(Path(__file__).resolve().parent.parent / "src")
for p in (_HOOKS, _SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

import _ticket_state as ts  # noqa: E402
from agent_factory import rubric_assembly as ra  # noqa: E402


class _Spy:
    def __init__(self):
        self.patches = []

    def patch_meta(self, cid, patch, **kw):
        self.patches.append((cid, patch))
        return {"id": cid, "meta": patch}


def _cand(cid, severity, axis):
    return {"id": cid, "meta": {"check_id": cid, "kind": "graded", "candidate": True,
                                "severity": severity,
                                "rubric": {"axes": [{"name": axis, "threshold": 0.8}]}}}


def test_assembler_output_pins_as_graded_validations(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(ts, "_praxis", spy)

    candidates = [_cand("crit", 3, "security"), _cand("minor-a", 1, "craft"),
                  _cand("minor-b", 1, "docs")]
    validations = ra.assemble(candidates, budget=1, covers=["R1"])

    # The assembler produced 1 promoted + 1 aggregate.
    assert ra.AGGREGATE_ID in {v["validation_id"] for v in validations}

    # Pin through the REAL engine path.
    ts.pin_validations("R1", validations)
    pinned = spy.patches[-1][1][ts.M_PINNED_CHECKS]

    # Every pinned entry is a graded validation carrying its frozen rubric (via _norm_validation).
    assert pinned, "expected pinned validations"
    for entry in pinned:
        assert entry["kind"] == "graded"
        assert isinstance(entry.get("rubric"), dict) and entry["rubric"].get("axes")
        assert entry["passed"] is None                       # unrun until VERIFY grades it

    # The high-severity candidate was promoted (its own rubric frozen), the low ones folded.
    promoted = [e for e in pinned if e["validation_id"] != ra.AGGREGATE_ID]
    assert len(promoted) == 1
    assert promoted[0]["source_check_id"] == "crit"
    assert promoted[0]["covers"] == ["R1"]

    agg = [e for e in pinned if e["validation_id"] == ra.AGGREGATE_ID][0]
    folded_axis_names = {a["name"] for a in agg["rubric"]["axes"]}
    assert any(n.startswith("minor-a:") for n in folded_axis_names)
    assert any(n.startswith("minor-b:") for n in folded_axis_names)


def test_empty_pool_pins_nothing_graded(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(ts, "_praxis", spy)
    ts.pin_validations("R1", ra.assemble([], budget=3, covers=["R1"]))
    assert spy.patches[-1][1][ts.M_PINNED_CHECKS] == []       # no candidates -> no graded validations
