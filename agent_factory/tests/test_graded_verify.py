"""U2/U5/U6: graded-check schema passthrough, VERIFY caching, and loop-termination guards."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _graded_verify as gv  # noqa: E402
import _ticket_state as ts  # noqa: E402

PLAN = ("team-app", "prd-team-app")

RUBRIC = {"confidence_floor": 5, "criterion": "c",
          "axes": [{"name": "a", "threshold": 0.7}]}


class FakePraxis:
    def __init__(self, meta):
        self._meta = dict(meta)

    def get_fact(self, cid, *, space=None, snapshot=None):
        return {"id": cid, "meta": dict(self._meta)}

    def patch_meta(self, cid, meta_dict, *, space=None, snapshot=None):
        self._meta.update(meta_dict)
        return {"id": cid, "meta": dict(self._meta)}


def _install(monkeypatch, meta):
    fake = FakePraxis(meta)
    monkeypatch.setattr(ts, "_praxis", fake)
    monkeypatch.setattr(ts._praxis, "facts_by", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(ts._praxis, "surface_checks", lambda *a, **k: [], raising=False)
    return fake


def _stub(passed_axis: float, defects=None):
    payload = {"axis_scores": {"a": passed_axis}, "defects": defects or []}
    calls = {"n": 0}

    def complete(_prompt):
        calls["n"] += 1
        return json.dumps(payload)

    complete.calls = calls  # type: ignore[attr-defined]
    return complete


# ---- U2: schema passthrough --------------------------------------------------

def test_norm_validation_carries_kind_and_frozen_rubric(monkeypatch):
    fake = _install(monkeypatch, {})
    ts.pin_validations("R1", [{"validation_id": "v1", "covers": ["R1"],
                               "kind": "graded", "rubric": RUBRIC}], ref=PLAN)
    entry = fake._meta[ts.M_PINNED_CHECKS][0]
    assert entry["kind"] == "graded" and entry["rubric"] == RUBRIC


def test_binary_validation_stays_byte_compatible(monkeypatch):
    fake = _install(monkeypatch, {})
    ts.pin_validations("R1", [{"validation_id": "v1", "covers": ["R1"], "run": "pytest"}], ref=PLAN)
    entry = fake._meta[ts.M_PINNED_CHECKS][0]
    assert "kind" not in entry and "rubric" not in entry


# ---- U5: verdict wiring + caching -------------------------------------------

def _pin_graded(fake):
    ts.pin_validations("R1", [{"validation_id": "v1", "covers": ["R1"],
                               "kind": "graded", "rubric": RUBRIC}], ref=PLAN)


def test_graded_pass_records_passed_true(monkeypatch):
    fake = _install(monkeypatch, {})
    _pin_graded(fake)
    r = gv.verify_graded_check("R1", "v1", "diffA", _stub(0.9), ref=PLAN, now=1.0)
    assert r.verdict.passed and not r.should_block
    entry = fake._meta[ts.M_PINNED_CHECKS][0]
    assert entry["passed"] is True and entry["source"] == gv.GRADED_SOURCE
    assert entry["verdict"]["code_hash"] == gv.code_state_hash("diffA")


def test_graded_fail_records_passed_false(monkeypatch):
    fake = _install(monkeypatch, {})
    _pin_graded(fake)
    r = gv.verify_graded_check("R1", "v1", "diffA", _stub(0.3), ref=PLAN, now=1.0)
    assert not r.verdict.passed
    assert fake._meta[ts.M_PINNED_CHECKS][0]["passed"] is False


def test_identical_code_reuses_cached_verdict_no_judge_call(monkeypatch):
    fake = _install(monkeypatch, {})
    _pin_graded(fake)
    stub = _stub(0.9)
    gv.verify_graded_check("R1", "v1", "same", stub, ref=PLAN, now=1.0)
    assert stub.calls["n"] == 1
    r2 = gv.verify_graded_check("R1", "v1", "same", stub, ref=PLAN, now=2.0)
    assert r2.cached and stub.calls["n"] == 1  # no second judge call
    assert r2.verdict.passed


def test_changed_code_recomputes(monkeypatch):
    fake = _install(monkeypatch, {})
    _pin_graded(fake)
    stub = _stub(0.9)
    gv.verify_graded_check("R1", "v1", "codeA", stub, ref=PLAN, now=1.0)
    gv.verify_graded_check("R1", "v1", "codeB", stub, ref=PLAN, now=2.0)
    assert stub.calls["n"] == 2


def test_frozen_rubric_read_from_pinned_entry_not_arg(monkeypatch):
    """No rubric arg -> uses the rubric frozen on the pinned validation."""
    fake = _install(monkeypatch, {})
    _pin_graded(fake)
    r = gv.verify_graded_check("R1", "v1", "d", _stub(0.9), ref=PLAN, now=1.0)
    assert r.verdict.passed


def test_graded_gate_integration_pass_then_finish(monkeypatch):
    fake = _install(monkeypatch, {ts.M_REQUIRED_VALIDATIONS: ["R1"]})
    _pin_graded(fake)
    gv.verify_graded_check("R1", "v1", "d", _stub(0.9), ref=PLAN, now=1.0)
    assert ts.all_validations_passed("R1", ref=PLAN) is True


def test_graded_fail_keeps_gate_closed(monkeypatch):
    fake = _install(monkeypatch, {ts.M_REQUIRED_VALIDATIONS: ["R1"]})
    _pin_graded(fake)
    gv.verify_graded_check("R1", "v1", "d", _stub(0.3), ref=PLAN, now=1.0)
    assert ts.all_validations_passed("R1", ref=PLAN) is False


# ---- U6: loop-termination guards --------------------------------------------

def test_iteration_cap_trips_block(monkeypatch):
    fake = _install(monkeypatch, {})
    _pin_graded(fake)
    d = [{"file": "x", "line": 1, "problem": "p", "remedy": "r", "confidence": 8}]
    # Code changes each round (no caching); defects strictly DECREASE so the convergence guard is
    # satisfied and only the iteration cap can trip the block.
    counts = [5, 3]
    for i, n in enumerate(counts, start=1):
        r = gv.verify_graded_check("R1", "v1", f"code{i}", _stub(0.3, d * n), ref=PLAN, now=float(i))
        assert not r.should_block
    r = gv.verify_graded_check("R1", "v1", "code3", _stub(0.3, d * 1), ref=PLAN, now=3.0)
    assert r.should_block and "cap" in r.block_reason


def test_non_convergence_trips_block_before_cap(monkeypatch):
    fake = _install(monkeypatch, {})
    _pin_graded(fake)
    d = [{"file": "x", "line": 1, "problem": "p", "remedy": "r", "confidence": 8}]
    gv.verify_graded_check("R1", "v1", "code1", _stub(0.3, d), ref=PLAN, now=1.0)  # 1 defect
    r = gv.verify_graded_check("R1", "v1", "code2", _stub(0.3, d), ref=PLAN, now=2.0)  # still 1
    assert r.should_block and "converging" in r.block_reason


def test_flapping_on_identical_code_consumes_no_iteration(monkeypatch):
    """The anti-flap guard: re-verifying unchanged code never advances the loop counter."""
    fake = _install(monkeypatch, {})
    _pin_graded(fake)
    stub = _stub(0.3, [{"file": "x", "line": 1, "problem": "p", "remedy": "r", "confidence": 8}])
    r1 = gv.verify_graded_check("R1", "v1", "frozen", stub, ref=PLAN, now=1.0)
    r2 = gv.verify_graded_check("R1", "v1", "frozen", stub, ref=PLAN, now=2.0)
    assert r1.iterations == 1 and r2.iterations == 1 and r2.cached
    assert not r2.should_block


def test_edited_library_does_not_move_target_midticket(monkeypatch):
    """Frozen rubric: the pinned entry's rubric is what VERIFY grades, regardless of the file."""
    fake = _install(monkeypatch, {})
    _pin_graded(fake)
    meta = ts._meta("R1", PLAN)
    frozen = gv.frozen_rubric_for(meta, "v1")
    assert frozen is not None and frozen.axes[0].threshold == 0.7  # from the pinned copy
