"""U3: wire ``promote_universal`` seeded checks into the MANDATORY lane — report-only + exemption.

A ``promote_universal=true`` seeded check injects as a mandatory graded validation requirement on
every NON-exempt ticket (tag-independent, incl. a tag-less ticket), carrying its serialized rubric so
a worker-synthesized validation covers it and ``verify_graded_check`` grades it. In ``report_only``
mode the requirement is pinned and its verdict recorded (calibration data) but EXCLUDED from
``all_validations_passed`` — it cannot block. Exempt tickets (``vendored``/``generated``/``config``
tag or ``meta.universal_exempt``) get none.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402
from agent_factory.rubric import rubric_from_dict  # noqa: E402
from agent_factory.seeded_checks import SeededCheck, load_seeded_checks, universal_seeded_checks  # noqa: E402


def _universal_check(check_id="minimalism-dry", report_only=True) -> SeededCheck:
    rubric = rubric_from_dict({
        "axes": [{"name": "minimalism", "threshold": 0.8, "guidance": "no dead code"}],
        "anchors": {"good": ["return a + b"], "slop": ["unused = a + b"]},
    })
    return SeededCheck(check_id=check_id, kind="graded", applies_to=("*",),
                       criterion="strict minimization", promote_universal=True,
                       rubric=rubric, report_only=report_only)


def _use(monkeypatch, *checks):
    monkeypatch.setattr(ts, "_universal_checks", lambda: list(checks))


# ------------------------------------------------------------------- seeded-library plumbing (U3)

def _write(tmp_path, body):
    p = tmp_path / "seeded_checks.toml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_universal_seeded_checks_filters_promote_flag_and_report_only(tmp_path):
    p = _write(tmp_path, """
        [[check]]
        check_id = "not-universal"
        kind = "graded"
        [[check.axes]]
        name = "a"
        threshold = 0.5

        [[check]]
        check_id = "the-universal"
        kind = "graded"
        promote_universal = true
        report_only = true
        [[check.axes]]
        name = "b"
        threshold = 0.7
    """)
    checks = load_seeded_checks(p)
    universal = universal_seeded_checks(checks)
    assert [c.check_id for c in universal] == ["the-universal"]
    assert universal[0].report_only is True
    assert {c.check_id for c in checks if not c.promote_universal} == {"not-universal"}


# ------------------------------------------------------------------- injection into the contract

def test_injects_on_every_non_exempt_ticket_including_tagless(monkeypatch):
    _use(monkeypatch, _universal_check())
    for tags in ([], ["backend"], ["auth", "ui"]):
        reqs = ts.contract_with_floor("R1", "acc", resolved=[], ticket_meta={"tags": tags})
        ids = [ts._check_id(r) for r in reqs]
        assert "minimalism-dry" in ids
        # It carries kind=graded + a serialized rubric + report_only, so VERIFY can grade it.
        u = next(r for r in reqs if ts._check_id(r) == "minimalism-dry")
        assert u["meta"]["kind"] == "graded"
        assert u["meta"]["rubric"]["axes"][0]["name"] == "minimalism"
        assert u["meta"]["report_only"] is True


def test_exempt_ticket_gets_no_universal_check(monkeypatch):
    _use(monkeypatch, _universal_check())
    for meta in ({"tags": ["generated"]}, {"tags": ["Vendored"]}, {"tags": ["config"]},
                 {"tags": [], "universal_exempt": True}):
        reqs = ts.contract_with_floor("R1", "acc", resolved=[], ticket_meta=meta)
        assert "minimalism-dry" not in [ts._check_id(r) for r in reqs]


def test_not_injected_onto_an_otherwise_empty_contract(monkeypatch):
    # No acceptance, no resolved checks -> the empty-contract BLOCK path must survive (a planning
    # defect), not be masked into buildable by a report-only universal.
    _use(monkeypatch, _universal_check())
    assert ts.contract_with_floor("R1", "", resolved=[], ticket_meta={"tags": []}) == []


def test_promote_universal_absent_is_byte_identical(monkeypatch):
    # No promote_universal checks -> injection is a no-op; identical to passing no ticket_meta.
    _use(monkeypatch)  # empty
    resolved = [{"id": "CHK-1"}]
    with_meta = ts.contract_with_floor("R1", "acc", resolved, ticket_meta={"tags": ["x"]})
    without = ts.contract_with_floor("R1", "acc", resolved)
    assert with_meta == without == [{"id": "R1::acceptance", "text": "acc",
                                     "meta": {"acceptance": "acc", "synthetic": "acceptance-floor",
                                              "verify": "automated"}}, {"id": "CHK-1"}]


def test_deterministic_and_deduped_across_passes(monkeypatch):
    _use(monkeypatch, _universal_check())
    a = ts.contract_with_floor("R1", "acc", resolved=[], ticket_meta={"tags": ["b"]})
    b = ts.contract_with_floor("R1", "acc", resolved=[], ticket_meta={"tags": ["b"]})
    assert a == b
    assert [ts._check_id(r) for r in a].count("minimalism-dry") == 1
    # Already present in the resolved set -> not duplicated.
    pre = [{"id": "minimalism-dry"}]
    reqs = ts.contract_with_floor("R1", "acc", pre, ticket_meta={"tags": ["b"]})
    assert [ts._check_id(r) for r in reqs].count("minimalism-dry") == 1


# ------------------------------------------------------------------- report-only vs gating math

def _fact(**meta):
    return {"id": "R1", "meta": meta}


def test_report_only_recorded_but_does_not_block():
    # required includes the universal, but it is report-only: neither coverage nor a passing
    # validation for it is required. A report-only validation that FAILS still does not block.
    f = _fact(required_validations=["R1::acceptance", "minimalism-dry"],
              report_only_requirements=["minimalism-dry"],
              pinned_checks=[
                  {"validation_id": "v-acc", "covers": ["R1::acceptance"], "passed": True},
                  {"validation_id": "v-min", "covers": ["minimalism-dry"], "passed": False},
              ])
    assert ts.all_validations_passed(f) is True


def test_report_only_uncovered_still_passes():
    # The offline/CI case: existing tests never author a validation for the universal req; with it
    # report-only, the ticket still finishes on its acceptance floor alone.
    f = _fact(required_validations=["R1::acceptance", "minimalism-dry"],
              report_only_requirements=["minimalism-dry"],
              pinned_checks=[{"validation_id": "v-acc", "covers": ["R1::acceptance"], "passed": True}])
    assert ts.coverage_gap(f) == ["minimalism-dry"]  # coverage_gap still SEES it (visibility)
    assert ts.all_validations_passed(f) is True       # ...but it does not gate


def test_report_only_false_gates():
    gating = ["R1::acceptance", "minimalism-dry"]
    # report_only_requirements empty -> the universal now GATES: uncovered -> not done.
    uncovered = _fact(required_validations=gating, report_only_requirements=[],
                      pinned_checks=[{"validation_id": "v-acc", "covers": ["R1::acceptance"],
                                      "passed": True}])
    assert ts.all_validations_passed(uncovered) is False
    # Cover it and pass it -> done.
    done = _fact(required_validations=gating, report_only_requirements=[],
                 pinned_checks=[{"validation_id": "v-acc", "covers": ["R1::acceptance"], "passed": True},
                                {"validation_id": "v-min", "covers": ["minimalism-dry"], "passed": True}])
    assert ts.all_validations_passed(done) is True


# ------------------------------------------------------------------- pin_requirements records ids

class _Spy:
    def __init__(self):
        self.patches = []

    def get_fact(self, cid, **kw):
        return {"id": cid, "meta": {}}

    def patch_meta(self, cid, patch, **kw):
        self.patches.append(patch)
        return {"id": cid, "meta": patch}


def test_pin_requirements_records_report_only_ids(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(ts, "_praxis", spy)
    _use(monkeypatch, _universal_check())
    reqs = ts.contract_with_floor("R1", "acc", resolved=[], ticket_meta={"tags": ["b"]})
    ts.pin_requirements("R1", reqs)
    patch = spy.patches[-1]
    assert "minimalism-dry" in patch[ts.M_REQUIRED_VALIDATIONS]
    assert patch[ts.M_REPORT_ONLY_REQUIREMENTS] == ["minimalism-dry"]
