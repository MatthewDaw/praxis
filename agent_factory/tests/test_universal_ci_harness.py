"""U4: the offline judge harness lets tickets carrying a universal graded check finish in CI.

Two guarantees:
  * REPORT-ONLY (the shipped default): the injected universal is graded + recorded but non-gating, so
    a ticket finishes on its acceptance floor alone — no judge needed. This is why every existing
    ticket-to-finished test stays green once ``minimalism-dry`` ships (it ships report-only).
  * GATING (the flipped state): with the stub ``Complete`` judge, a graded universal validation is
    graded, its pass recorded, and the ticket reaches ``all_validations_passed`` — proving the
    graded VERIFY path works end-to-end offline.

Note: once a universal is flipped to GATING (``report_only=false``), the "no-universal byte-identical"
property of ``contract_with_floor``/``start_ticket`` no longer holds — the gate is the point.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _graded_verify as gv  # noqa: E402
import _ticket_state as ts  # noqa: E402
from agent_factory.rubric import rubric_from_dict  # noqa: E402
from agent_factory.seeded_checks import SeededCheck  # noqa: E402

PLAN = ("team-app", "prd-team-app")


class FakePraxis:
    """Persists one ticket's meta across get_fact/patch_meta (MERGE), like the live server's PATCH."""

    def __init__(self, meta):
        self._meta = dict(meta)

    def get_fact(self, cid, *, space=None, snapshot=None):
        return {"id": cid, "meta": dict(self._meta)}

    def patch_meta(self, cid, meta_dict, *, space=None, snapshot=None):
        self._meta.update(meta_dict)
        return {"id": cid, "meta": dict(self._meta)}

    def facts_by(self, category=None, meta=None, state="active", space=None, snapshot=None, **k):
        return []  # no declared Praxis checks -> only the acceptance floor + the universal lane

    def surface_checks(self, project, screen_id, scope=None, space=None, snapshot=None):
        return []


def _universal(report_only: bool) -> SeededCheck:
    rubric = rubric_from_dict({
        "axes": [{"name": "minimalism", "threshold": 0.8, "guidance": "no dead code or dup"}],
        "anchors": {"good": ["return a + b"], "slop": ["unused = a + b"]},
    })
    return SeededCheck(check_id="minimalism-dry", kind="graded", applies_to=("*",),
                       criterion="strict minimization", promote_universal=True,
                       rubric=rubric, report_only=report_only)


def _install(monkeypatch, report_only: bool):
    fake = FakePraxis({"requirement_id": "R1", "tags": [], "acceptance": "it works",
                       "verify": "automated"})
    monkeypatch.setattr(ts, "_praxis", fake)
    monkeypatch.setattr(ts, "_universal_checks", lambda: [_universal(report_only)])
    return fake


def test_report_only_universal_does_not_block_finish(monkeypatch):
    fake = _install(monkeypatch, report_only=True)
    reqs = ts.start_ticket("R1", "owner", project="team-app")
    ids = {ts._check_id(r) for r in reqs}
    assert {"R1::acceptance", "minimalism-dry"} <= ids                 # injected...
    assert fake._meta[ts.M_REPORT_ONLY_REQUIREMENTS] == ["minimalism-dry"]  # ...as report-only

    # The worker covers ONLY the acceptance floor (no judge run) and finishes.
    ts.pin_validations("R1", [{"validation_id": "v-acc", "covers": ["R1::acceptance"],
                               "run": "pytest -q"}], ref=PLAN)
    ts.record_validation_pass("R1", "v-acc", True, ref=PLAN)
    assert ts.all_validations_passed("R1", ref=PLAN) is True


def test_stub_judge_passes_a_gating_universal(monkeypatch, pass_judge):
    fake = _install(monkeypatch, report_only=False)
    ts.start_ticket("R1", "owner", project="team-app")
    assert fake._meta[ts.M_REPORT_ONLY_REQUIREMENTS] == []             # gating, not report-only

    # Worker covers the floor AND authors a graded validation for the universal (carrying its rubric).
    ustub = _universal(report_only=False)
    from agent_factory.rubric import rubric_to_dict
    ts.pin_validations("R1", [
        {"validation_id": "v-acc", "covers": ["R1::acceptance"], "run": "pytest -q"},
        {"validation_id": "v-min", "covers": ["minimalism-dry"], "kind": "graded",
         "rubric": rubric_to_dict(ustub.rubric), "source_check_id": "minimalism-dry"},
    ], ref=PLAN)
    ts.record_validation_pass("R1", "v-acc", True, ref=PLAN)

    # Grade the universal with the OFFLINE stub judge — records a pass via the normal sink.
    res = gv.verify_graded_check("R1", "v-min", "def f():\n    return a + b\n", pass_judge, ref=PLAN)
    assert res.verdict.passed and not res.should_block
    assert ts.all_validations_passed("R1", ref=PLAN) is True


def test_stub_fail_judge_blocks_a_gating_universal(monkeypatch, fail_judge):
    _install(monkeypatch, report_only=False)
    ts.start_ticket("R1", "owner", project="team-app")
    ustub = _universal(report_only=False)
    from agent_factory.rubric import rubric_to_dict
    ts.pin_validations("R1", [
        {"validation_id": "v-acc", "covers": ["R1::acceptance"], "run": "pytest -q"},
        {"validation_id": "v-min", "covers": ["minimalism-dry"], "kind": "graded",
         "rubric": rubric_to_dict(ustub.rubric), "source_check_id": "minimalism-dry"},
    ], ref=PLAN)
    ts.record_validation_pass("R1", "v-acc", True, ref=PLAN)

    res = gv.verify_graded_check("R1", "v-min", "def f():\n    unused = a + b\n", fail_judge, ref=PLAN)
    assert not res.verdict.passed
    # The gating universal failed -> the ticket is NOT done.
    assert ts.all_validations_passed("R1", ref=PLAN) is False
