"""Locks the checks-space seam in ``hooks/_ticket_state.py`` + ``hooks/_praxis.py``:

Check RESOLUTION reads validation rules from a DEDICATED tenancy space (the checks-space), while
ticket STATE stays in the default/plan space. Defaults are per-scope (validation -> coding-validation,
planning -> planning-validation); the skills override per-invocation via ``checks_space=``.

These tests capture the ``space`` kwarg handed to the check-read lanes (``facts_by`` /
``surface_checks``) — no network — so they assert exactly which space each resolve reads from.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _praxis  # noqa: E402
import _ticket_state as ts  # noqa: E402


class _SpyPraxis:
    """Records the ``space`` each check-read lane was called with; returns canned checks."""

    def __init__(self, checks=None, surface=None):
        self._checks = checks or []
        self._surface = surface or []
        self.facts_by_spaces = []
        self.surface_spaces = []

    def facts_by(self, category=None, meta=None, state="active", space=None):
        self.facts_by_spaces.append(space)
        return list(self._checks)

    def surface_checks(self, project, screen_id, scope=None, space=None):
        self.surface_spaces.append(space)
        return list(self._surface)

    # ticket reads that resolve may touch (only when a bare id is passed) — unused here.
    def get_fact(self, cid):  # pragma: no cover
        return {"id": cid, "meta": {}}


def _spy(monkeypatch, **kw):
    spy = _SpyPraxis(**kw)
    monkeypatch.setattr(ts, "_praxis", spy)
    return spy


# --------------------------------------------------------------------------- the default mapping

def test_default_checks_space_mapping():
    assert ts.default_checks_space("validation") == "coding-validation"
    assert ts.default_checks_space("planning") == "planning-validation"
    assert ts.default_checks_space(None) is None       # back-compat: ticket/default space
    assert ts.default_checks_space("other") is None


# --------------------------------------------------------------------------- planning lane (af-intake)

def test_planning_scope_defaults_to_plan_validation_space(monkeypatch):
    spy = _spy(monkeypatch, checks=[{"id": "L1", "scope": "planning"}])
    out = ts.resolve_validation_requirements({"id": "plan", "meta": {}}, scope="planning")
    assert [c["id"] for c in out] == ["L1"]
    assert spy.facts_by_spaces == ["planning-validation"]


# --------------------------------------------------------------------------- validation lane (af-build)

def test_validation_scope_defaults_to_coding_validation_space(monkeypatch):
    spy = _spy(monkeypatch,
               checks=[{"id": "C1", "scope": "validation", "meta": {"applies_to": ["auth"]}}],
               surface=[{"id": "C2", "scope": "validation"}])
    ticket = {"id": "R1", "meta": {"tags": ["auth"], "surfaces": ["s-login"]}}
    ts.resolve_validation_requirements(ticket, project="team-app", scope="validation")
    # every check read (tag lane + the "*" wildcard pull + surface lane) reads from coding-validation
    assert spy.facts_by_spaces and all(s == "coding-validation" for s in spy.facts_by_spaces)
    assert spy.surface_spaces == ["coding-validation"]


# --------------------------------------------------------------------------- explicit overrides

def test_explicit_checks_space_overrides_default(monkeypatch):
    spy = _spy(monkeypatch, checks=[{"id": "L1", "scope": "planning"}])
    ts.resolve_validation_requirements({"id": "plan", "meta": {}}, scope="planning",
                                       checks_space="my-lenses")
    assert spy.facts_by_spaces == ["my-lenses"]


def test_explicit_none_forces_ticket_default_space(monkeypatch):
    spy = _spy(monkeypatch, checks=[{"id": "C1", "scope": "validation",
                                     "meta": {"applies_to": ["auth"]}}])
    ticket = {"id": "R1", "meta": {"tags": ["auth"]}}
    ts.resolve_validation_requirements(ticket, project="team-app", scope="validation",
                                       checks_space=None)
    # None override => no per-request space header on any check read
    assert spy.facts_by_spaces and all(s is None for s in spy.facts_by_spaces)


def test_scope_none_backcompat_reads_ticket_space(monkeypatch):
    spy = _spy(monkeypatch, checks=[{"id": "C1", "meta": {"applies_to": ["auth"]}}])
    ticket = {"id": "R1", "meta": {"tags": ["auth"]}}
    ts.resolve_validation_requirements(ticket, project="team-app")  # scope=None
    assert spy.facts_by_spaces and all(s is None for s in spy.facts_by_spaces)


# --------------------------------------------------------------------------- transport: per-request header

def test_request_space_overrides_header(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        captured["space"] = req.headers.get("X-praxis-space")
        return _Resp()

    monkeypatch.setattr(_praxis, "_auth_headers", lambda: {})
    monkeypatch.setattr(_praxis.urllib.request, "urlopen", fake_urlopen)
    _praxis.facts_by(category="check", space="coding-validation")
    assert captured["space"] == "coding-validation"
