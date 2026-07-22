"""Locks U1: the episode + contradiction wrappers on ``hooks/_praxis.py``.

These thin wrappers let a Stop-hook subprocess and ``tools/plan_gate_check.py`` read/write the same
decision-log + contradiction lanes the MCP tools (``praxis_record_episode`` /
``praxis_get_contradictions``) call, WITHOUT importing the praxis package. We stub the ONE transport
seam (``_praxis._request``) and assert each wrapper (a) issues the right method/path/params/body and
(b) parses the response — plus that ``PraxisUnreachable`` propagates (fail-closed) and empty results
come back clean.
"""

import sys
from pathlib import Path

import pytest

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _praxis  # noqa: E402
from _praxis import PraxisUnreachable  # noqa: E402


class _SpyRequest:
    """Records every ``_request`` call and returns a canned payload (or raises)."""

    def __init__(self, result=None, raise_exc=None):
        self._result = {} if result is None else result
        self._raise = raise_exc
        self.calls = []  # (method, path, params, body, space, snapshot, not_found_ok)

    def __call__(self, method, path, *, params=None, body=None, not_found_ok=False,
                 space=None, snapshot=None):
        self.calls.append({
            "method": method, "path": path, "params": params, "body": body,
            "space": space, "snapshot": snapshot, "not_found_ok": not_found_ok,
        })
        if self._raise is not None:
            raise self._raise
        return self._result


def _spy(monkeypatch, **kw):
    spy = _SpyRequest(**kw)
    monkeypatch.setattr(_praxis, "_request", spy)
    return spy


# --------------------------------------------------------------------------- record_episode

def test_record_episode_posts_insights_with_episode_payload(monkeypatch):
    spy = _spy(monkeypatch, result={"id": "f1", "summary": "recorded episode"})
    out = _praxis.record_episode(
        "signed the contract",
        episode={"kind": "contract-signed", "n_assertions": 12,
                 "actions": {"cut": 2, "merged": 1, "added": 3}, "signer": "evaluator"},
        derived_from=["R1", "R2"],
    )
    assert out == {"id": "f1", "summary": "recorded episode"}
    call = spy.calls[-1]
    assert call["method"] == "POST"
    assert call["path"] == "/insights"
    assert call["body"]["insight"] == "signed the contract"
    assert call["body"]["category"] == "episodic"
    ep = call["body"]["meta"]["episode"]
    assert ep["kind"] == "contract-signed"
    assert ep["signer"] == "evaluator"
    assert ep["actions"] == {"cut": 2, "merged": 1, "added": 3}
    # store-only defaults + the derived_from edge the MCP tool also carries.
    assert ep["outcome"] == "pending"
    assert call["body"]["derivedFrom"] == ["R1", "R2"]


def test_record_episode_defaults_and_overrides(monkeypatch):
    spy = _spy(monkeypatch, result={})
    _praxis.record_episode("d", outcome="succeeded", alternatives=["a", "b"], decided_at="2026-07-22")
    ep = spy.calls[-1]["body"]["meta"]["episode"]
    assert ep["outcome"] == "succeeded"
    assert ep["alternatives"] == ["a", "b"]
    assert ep["decided_at"] == "2026-07-22"
    # no derived_from -> the key is omitted entirely.
    assert "derivedFrom" not in spy.calls[-1]["body"]


def test_record_episode_propagates_unreachable(monkeypatch):
    _spy(monkeypatch, raise_exc=PraxisUnreachable("down"))
    with pytest.raises(PraxisUnreachable):
        _praxis.record_episode("d")


# --------------------------------------------------------------------------- get_episodes

def test_get_episodes_queries_facts_by_episodic(monkeypatch):
    spy = _spy(monkeypatch, result={"facts": [{"id": "e1"}, {"id": "e2"}]})
    out = _praxis.get_episodes(meta={"kind": "contract-signed"},
                               space="team-app", snapshot="prd-team-app")
    assert [f["id"] for f in out] == ["e1", "e2"]
    call = spy.calls[-1]
    assert call["method"] == "GET"
    assert call["path"] == "/facts/by"
    assert call["params"]["category"] == "episodic"
    assert call["params"]["state"] == "active"
    assert '"kind": "contract-signed"' in call["params"]["meta"] or \
        '"kind":"contract-signed"' in call["params"]["meta"]
    assert call["space"] == "team-app" and call["snapshot"] == "prd-team-app"


def test_get_episodes_empty_returns_list(monkeypatch):
    _spy(monkeypatch, result={})
    assert _praxis.get_episodes() == []


def test_get_episodes_propagates_unreachable(monkeypatch):
    _spy(monkeypatch, raise_exc=PraxisUnreachable("down"))
    with pytest.raises(PraxisUnreachable):
        _praxis.get_episodes()


# --------------------------------------------------------------------------- get_contradictions

def test_get_contradictions_reads_endpoint_and_parses_list(monkeypatch):
    spy = _spy(monkeypatch, result=[{"pair_id": "p1"}, {"pair_id": "p2"}])
    out = _praxis.get_contradictions(space="team-app", snapshot="planning-validation")
    assert [c["pair_id"] for c in out] == ["p1", "p2"]
    call = spy.calls[-1]
    assert call["method"] == "GET"
    assert call["path"] == "/contradictions"
    assert call["space"] == "team-app" and call["snapshot"] == "planning-validation"


def test_get_contradictions_empty_returns_list(monkeypatch):
    _spy(monkeypatch, result=[])
    assert _praxis.get_contradictions() == []
    # a dict-wrapped shape is also tolerated.
    _spy(monkeypatch, result={})
    assert _praxis.get_contradictions() == []


def test_get_contradictions_propagates_unreachable(monkeypatch):
    _spy(monkeypatch, raise_exc=PraxisUnreachable("down"))
    with pytest.raises(PraxisUnreachable):
        _praxis.get_contradictions()


# --------------------------------------------------------------------------- get_fact not_found_ok

def test_get_fact_not_found_ok_threads_through(monkeypatch):
    spy = _spy(monkeypatch, result={})
    _praxis.get_fact("prd-team-app::planning", space="team-app", snapshot="prd-team-app",
                     not_found_ok=True)
    call = spy.calls[-1]
    assert call["path"] == "/candidates/prd-team-app::planning"
    assert call["not_found_ok"] is True
