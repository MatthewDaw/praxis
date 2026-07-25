"""Locks U4: the ``R-CONTRACT-SIGNED`` rule + its threading through ``plan_gate_check``.

A blessed plan requires a signed contract whose evaluator ACTIONS were recorded (anti-Goodhart —
the count is informational, not the gate). The rule lives in the PURE ``evaluate_plan`` (contract
threaded IN, never read there); ``plan_gate_check`` reads the ``contract-signed`` episode via the U1
wrapper and supplies the field. A padded-count-but-no-actions contract still rejects.
"""

import ast
import sys
from pathlib import Path

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


def test_contract_none_fails_closed():
    # THE FIX: absent evidence is a REJECTION, not a stand-down. The rule used to fire only when a
    # contract WAS supplied, so a plan that never had one negotiated at all — the more dangerous
    # state — was admitted with zero reasons and af-intake-plan's B9 hard gate was trivially clearable.
    v = evaluate_plan(_reqs(), project="sotos", contract=None)
    assert v.admitted is False
    assert R_CONTRACT_SIGNED in v.rule_ids
    assert any("NO contract evidence at all" in r.message for r in v.reasons)


def test_empty_contract_fails_closed_as_absent():
    # `{}` carries no evidence either: same "no contract at all" message, not "unsigned".
    v = evaluate_plan(_reqs(), project="sotos", contract={})
    assert v.admitted is False
    assert any("NO contract evidence at all" in r.message for r in v.reasons)


def test_absent_message_distinguishes_from_signed_but_lazy():
    # The operator must be able to tell WHICH state the plan is in from the reason alone.
    absent = evaluate_plan(_reqs(), project="sotos", contract=None).reasons[0].message
    lazy = evaluate_plan(
        _reqs(), project="sotos", contract={"signed": True, "actions_recorded": False}
    ).reasons[0].message
    assert absent != lazy
    assert "NO contract evidence at all" in absent
    assert "no evaluator actions" in lazy.lower()


# --------------------------------------------------- raw build_signed_payload shape is understood

def _raw(*, kind="contract-signed", actions):
    # The shape contract_signature.build_signed_payload emits (and the shape a caller holding the
    # episode payload threads straight through), as opposed to read_contract's reduced form.
    return {"kind": kind, "n_assertions": 75, "actions": actions, "signer": "evaluator"}


def test_raw_payload_with_real_actions_admits():
    v = evaluate_plan(_reqs(), project="sotos", contract=_raw(actions={"cut": 4, "added": 1}))
    assert v.admitted is True
    assert R_CONTRACT_SIGNED not in v.rule_ids


def test_raw_payload_wrong_kind_rejects_as_unsigned():
    # A mislabelled payload is not a signed contract, however many actions it claims.
    v = evaluate_plan(_reqs(), project="sotos",
                      contract=_raw(kind="plan-reviewed", actions={"cut": 4}))
    assert v.admitted is False
    assert R_CONTRACT_SIGNED in v.rule_ids
    assert any("no signed contract" in r.message for r in v.reasons)


def test_raw_payload_all_zero_actions_still_rejects():
    # Anti-Goodhart preserved on the raw shape: a fat n_assertions count with zero real actions
    # is a signature over an unchanged draft.
    v = evaluate_plan(_reqs(), project="sotos",
                      contract=_raw(actions={"cut": 0, "merged": 0, "added": 0}))
    assert v.admitted is False
    assert any("no evaluator actions" in r.message.lower() for r in v.reasons)


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
    # The live "never negotiated" state gets the ABSENT reason, not "unsigned or malformed".
    assert any("NO contract evidence at all" in r.message for r in verdict.reasons)


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
    # No signed episode -> EMPTY evidence, which evaluate_plan treats as "no contract at all".
    assert pgc.read_contract("sotos") == {}
