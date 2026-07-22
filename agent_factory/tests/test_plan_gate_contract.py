"""Locks U4: the ``R-CONTRACT-SIGNED`` rule + its threading through ``plan_gate_check``.

A blessed plan requires a signed contract whose evaluator ACTIONS were recorded (anti-Goodhart —
the count is informational, not the gate). The rule lives in the PURE ``evaluate_plan`` (contract
threaded IN, never read there); ``plan_gate_check`` reads the ``contract-signed`` episode via the U1
wrapper and supplies the field. A padded-count-but-no-actions contract still rejects.
"""

import ast
import sys
from pathlib import Path

import pytest

_TOOLS = str(Path(__file__).resolve().parent.parent / "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

from agent_factory.plan_gate import (  # noqa: E402
    R_CONTRACT_SIGNED,
    Requirement,
    evaluate_plan,
)

import plan_gate_check as pgc  # noqa: E402


def _reqs():
    return [Requirement(id="R1", text="login works", acceptance="login succeeds end to end",
                        source="prd-sotos")]


# --------------------------------------------------------------------------- evaluate_plan rule

def test_signed_with_actions_admits():
    v = evaluate_plan(_reqs(), project="sotos",
                      contract={"signed": True, "actions_recorded": True})
    assert v.admitted is True
    assert R_CONTRACT_SIGNED not in v.rule_ids


def test_unsigned_rejects_with_rule():
    v = evaluate_plan(_reqs(), project="sotos",
                      contract={"signed": False, "actions_recorded": False})
    assert v.admitted is False
    assert R_CONTRACT_SIGNED in v.rule_ids
    assert any("no signed contract" in r.message for r in v.reasons)


def test_signed_but_no_actions_padded_count_rejects():
    v = evaluate_plan(_reqs(), project="sotos",
                      contract={"signed": True, "actions_recorded": False})
    assert v.admitted is False
    assert R_CONTRACT_SIGNED in v.rule_ids
    assert any("no evaluator actions" in r.message.lower() for r in v.reasons)


def test_contract_none_stands_down_backcompat():
    # The pure-eval / back-compat lane: no contract supplied -> the rule does not fire.
    v = evaluate_plan(_reqs(), project="sotos", contract=None)
    assert R_CONTRACT_SIGNED not in v.rule_ids
    assert v.admitted is True


# --------------------------------------------------------------------------- evaluate_plan is PURE

def test_plan_gate_module_has_no_praxis_import():
    src = (Path(__file__).resolve().parent.parent / "src" / "agent_factory" / "plan_gate.py").read_text()
    tree = ast.parse(src)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [n.name for n in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not any("praxis" in (m or "").lower() for m in imported), imported


# --------------------------------------------------------------------------- plan_gate_check threads it

def _fact(rid="R1"):
    return {"id": rid, "text": f"{rid} works", "source": "prd-sotos",
            "meta": {"requirement_id": rid, "acceptance": "it works end to end",
                     "tags": ["impl"], "verify": "automated"}}


class _FakePraxis:
    def __init__(self, episodes):
        self._episodes = episodes

    def facts_by(self, category=None, space=None, snapshot=None, **k):
        return [_fact()]

    def get_episodes(self, *, meta=None, space=None, snapshot=None):
        return list(self._episodes)


def _signed_episode(*, actions):
    return {"id": "ep", "meta": {"episode": {"kind": "contract-signed", "n_assertions": 12,
                                             "actions": actions, "signer": "evaluator"}}}


def test_check_plan_admits_when_signed_episode_has_actions(monkeypatch):
    monkeypatch.setattr(pgc, "_praxis", _FakePraxis([_signed_episode(actions={"cut": 1})]))
    verdict, _ = pgc.check_plan("sotos")
    assert verdict.admitted is True


def test_check_plan_rejects_when_no_signed_episode(monkeypatch):
    monkeypatch.setattr(pgc, "_praxis", _FakePraxis([]))
    verdict, _ = pgc.check_plan("sotos")
    assert verdict.admitted is False
    assert R_CONTRACT_SIGNED in verdict.rule_ids


def test_check_plan_rejects_when_signed_but_no_actions(monkeypatch):
    monkeypatch.setattr(pgc, "_praxis",
                        _FakePraxis([_signed_episode(actions={"cut": 0, "merged": 0, "added": 0})]))
    verdict, _ = pgc.check_plan("sotos")
    assert verdict.admitted is False
    assert R_CONTRACT_SIGNED in verdict.rule_ids


def test_read_contract_reduces_episodes(monkeypatch):
    monkeypatch.setattr(pgc, "_praxis", _FakePraxis([_signed_episode(actions={"added": 2})]))
    assert pgc.read_contract("sotos") == {"signed": True, "actions_recorded": True}
    monkeypatch.setattr(pgc, "_praxis", _FakePraxis([]))
    assert pgc.read_contract("sotos") == {"signed": False, "actions_recorded": False}
