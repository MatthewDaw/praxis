"""Locks item 3: the MECHANICAL plan gate (``agent_factory/tools/plan_gate_check.py``).

The tool reads the LIVE ``prd-<project>`` requirement facts, maps each onto a plan-gate
:class:`Requirement`, runs ``evaluate_plan``, and exits 0=admitted / 1=rejected / 2=cannot-run
(Praxis unreachable OR an empty plan — never a vacuous PASS).

We monkeypatch ``pgc._praxis`` with a fake whose ``facts_by`` (a) ASSERTS it is queried with
``category="requirement", space=<bare>, snapshot="prd-<bare>"`` and (b) returns canned requirement
facts, so the whole read→map→evaluate→exit-code path is asserted deterministically with no network.
"""

import sys
from pathlib import Path

import pytest

_TOOLS = str(Path(__file__).resolve().parent.parent / "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import plan_gate_check as pgc  # noqa: E402


class FakePraxis:
    """Fake exposing the ONE method the tool calls. ``facts_by`` asserts the exact live-plan query
    (category/space/snapshot) then returns canned facts — or raises, to model Praxis being down."""

    def __init__(self, facts=None, raise_exc=None):
        self._facts = [] if facts is None else list(facts)
        self._raise = raise_exc
        self.calls = []

    def facts_by(self, category=None, space=None, snapshot=None, **k):
        self.calls.append((category, space, snapshot))
        # the tool must read live requirement facts from the project's prd-<bare> snapshot.
        assert category == "requirement"
        assert space == "sotos"
        assert snapshot == "prd-sotos"
        if self._raise is not None:
            raise self._raise
        return list(self._facts)


def _fact(rid, *, acceptance, tags, verify, decision="", depends_on=None, source="prd-sotos"):
    return {
        "id": rid,
        "text": f"{rid} does its thing",
        "source": source,
        "meta": {
            "requirement_id": rid,
            "acceptance": acceptance,
            "tags": list(tags),
            "verify": verify,
            "decision": decision,
            "depends_on": list(depends_on or []),
        },
    }


# A MALFORMED plan: an IMPL-tagged decision (tags ["cdk","cognito"], NO architecture-decision tag)
# recognized ONLY by its meta.decision marker, verify=automated (an impl end-state), and an impl
# ticket R1 that depends_on it. Both decision rules must fire.
def _malformed():
    return [
        _fact("D1", acceptance="the cognito pool is provisioned", tags=["cdk", "cognito"],
              verify="automated", decision="human-decided"),
        _fact("R1", acceptance="login works end to end", tags=["impl"], verify="automated",
              depends_on=["D1"]),
    ]


# A WELL-FORMED plan: D1 is a manual, decision-level acceptance decision NOTHING depends on; R1 is a
# normal impl ticket depending on nothing. Every mechanical rule passes.
def _wellformed():
    return [
        _fact("D1", acceptance="the team accepts the cdk + cognito design", tags=["cdk", "cognito"],
              verify="manual", decision="human-decided"),
        _fact("R1", acceptance="login works end to end", tags=["impl"], verify="automated"),
    ]


def _install(monkeypatch, **kw):
    fake = FakePraxis(**kw)
    monkeypatch.setattr(pgc, "_praxis", fake)
    return fake


# --------------------------------------------------------------------------- read query + malformed

def test_facts_read_from_prd_snapshot_and_malformed_plan_rejects(monkeypatch):
    fake = _install(monkeypatch, facts=_malformed())
    verdict, requirements = pgc.check_plan("sotos")
    # the fake ASSERTED (category, space, snapshot); confirm it was actually queried.
    assert fake.calls == [("requirement", "sotos", "prd-sotos")]
    assert len(requirements) == 2
    assert verdict.admitted is False
    rule_ids = {r.rule_id for r in verdict.reasons}
    assert "R-DECISION-NOT-END-STATE" in rule_ids
    assert "R-NO-IMPL-DEPENDS-ON-DECISION" in rule_ids


def test_main_rejects_malformed_plan_and_prints_reasons_to_stderr(monkeypatch, capsys):
    _install(monkeypatch, facts=_malformed())
    rc = pgc.main(["sotos"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "R-DECISION-NOT-END-STATE" in err
    assert "R-NO-IMPL-DEPENDS-ON-DECISION" in err


# --------------------------------------------------------------------------- well-formed admits

def test_wellformed_plan_admits(monkeypatch):
    _install(monkeypatch, facts=_wellformed())
    verdict, requirements = pgc.check_plan("sotos")
    assert verdict.admitted is True
    assert verdict.reasons == []


def test_main_admits_wellformed_plan(monkeypatch, capsys):
    _install(monkeypatch, facts=_wellformed())
    rc = pgc.main(["sotos"])
    assert rc == 0
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------- no vacuous pass

def test_zero_facts_raises_valueerror_and_main_returns_2(monkeypatch, capsys):
    _install(monkeypatch, facts=[])
    with pytest.raises(ValueError):
        pgc.check_plan("sotos")
    # main must NOT report a vacuous PASS: exit 2, error on stderr.
    _install(monkeypatch, facts=[])
    rc = pgc.main(["sotos"])
    assert rc == 2
    assert "error" in capsys.readouterr().err.lower()


# --------------------------------------------------------------------------- Praxis unreachable

def test_praxis_unreachable_main_returns_2(monkeypatch, capsys):
    _install(monkeypatch, raise_exc=pgc.PraxisUnreachable("connection refused"))
    rc = pgc.main(["sotos"])
    assert rc == 2
    assert "unreachable" in capsys.readouterr().err.lower()
