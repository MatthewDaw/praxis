"""Locks the checks-resolution seam in ``hooks/_ticket_state.py`` + ``hooks/_praxis.py``:

Under the org -> space -> snapshot tenancy model every project IS a space (``space == <project>``).
Check RESOLUTION reads validation rules from a dedicated SNAPSHOT inside that project space, while
ticket STATE lives in the project's own ``prd-<project>`` snapshot. Snapshot defaults are per-scope
(validation -> ``building-validation``, planning -> ``planning-validation``), all built by the typed
``project_ref``; the skills override per-invocation via an explicit ``(space, snapshot)``
``override=`` pair.

These tests capture the ``(space, snapshot)`` pair handed to the check-read lanes (``facts_by`` /
``surface_checks``) — no network — so they assert exactly which space+snapshot each resolve reads
from.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _praxis  # noqa: E402
import _ticket_state as ts  # noqa: E402


class _SpyPraxis:
    """Records the ``(space, snapshot)`` each check-read lane was called with; returns canned checks."""

    def __init__(self, checks=None, surface=None):
        self._checks = checks or []
        self._surface = surface or []
        self.facts_by_spaces = []
        self.facts_by_snapshots = []
        self.surface_spaces = []
        self.surface_snapshots = []

    def facts_by(self, category=None, meta=None, state="active", space=None, snapshot=None):
        self.facts_by_spaces.append(space)
        self.facts_by_snapshots.append(snapshot)
        return list(self._checks)

    def surface_checks(self, project, screen_id, scope=None, space=None, snapshot=None):
        self.surface_spaces.append(space)
        self.surface_snapshots.append(snapshot)
        return list(self._surface)

    # ticket reads that resolve may touch (only when a bare id is passed) — unused here.
    def get_fact(self, cid):  # pragma: no cover
        return {"id": cid, "meta": {}}


def _spy(monkeypatch, **kw):
    spy = _SpyPraxis(**kw)
    monkeypatch.setattr(ts, "_praxis", spy)
    return spy


# --------------------------------------------------------------------------- the typed mapping

def test_project_ref_maps_each_scope_to_its_snapshot():
    ref = ts.project_ref("team-app")
    assert ref.plan == ("team-app", "prd-team-app")
    assert ref.validation == ("team-app", "building-validation")
    assert ref.planning == ("team-app", "planning-validation")
    assert ref.for_scope("validation") == ("team-app", "building-validation")
    assert ref.for_scope("planning") == ("team-app", "planning-validation")


def test_project_ref_strips_leading_prd_prefix():
    # A caller may pass either the bare project or the prd-<project> snapshot name.
    assert ts.project_ref("prd-team-app").plan == ("team-app", "prd-team-app")


def test_for_scope_rejects_unsupported_scope():
    import pytest
    for bad in (None, "other", ""):
        with pytest.raises(ValueError):
            ts.project_ref("team-app").for_scope(bad)


# --------------------------------------------------------------------------- planning lane (af-intake-plan)

def test_planning_scope_defaults_to_planning_validation_snapshot(monkeypatch):
    spy = _spy(monkeypatch, checks=[{"id": "L1", "scope": "planning"}])
    out = ts.resolve_validation_requirements({"id": "plan", "meta": {}}, project="team-app",
                                             scope="planning")
    assert [c["id"] for c in out] == ["L1"]
    # planning checks live in the project's OWN space, snapshot planning-validation
    assert spy.facts_by_spaces == ["team-app"]
    assert spy.facts_by_snapshots == ["planning-validation"]


# --------------------------------------------------------------------------- validation lane (af-build)

def test_validation_scope_defaults_to_building_validation_snapshot(monkeypatch):
    spy = _spy(monkeypatch,
               checks=[{"id": "C1", "scope": "validation", "meta": {"applies_to": ["auth"]}}],
               surface=[{"id": "C2", "scope": "validation"}])
    ticket = {"id": "R1", "meta": {"tags": ["auth"], "surfaces": ["s-login"]}}
    ts.resolve_validation_requirements(ticket, project="team-app", scope="validation")
    # every check read (tag lane + the "*" wildcard pull + surface lane) reads the project space's
    # building-validation snapshot
    assert spy.facts_by_spaces and all(s == "team-app" for s in spy.facts_by_spaces)
    assert spy.facts_by_snapshots and all(s == "building-validation" for s in spy.facts_by_snapshots)
    assert spy.surface_spaces == ["team-app"]
    assert spy.surface_snapshots == ["building-validation"]


# --------------------------------------------------------------------------- explicit overrides

def test_explicit_override_pair_overrides_space_and_snapshot(monkeypatch):
    spy = _spy(monkeypatch, checks=[{"id": "L1", "scope": "planning"}])
    ts.resolve_validation_requirements({"id": "plan", "meta": {}}, project="team-app",
                                       scope="planning",
                                       override=("shared-lenses", "planning-validation"))
    # an explicit (space, snapshot) override pair replaces BOTH halves of the default
    assert spy.facts_by_spaces == ["shared-lenses"]
    assert spy.facts_by_snapshots == ["planning-validation"]


# --------------------------------------------------------------------------- transport: tenancy headers

def test_request_emits_both_tenancy_headers(monkeypatch):
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
        captured["snapshot"] = req.headers.get("X-praxis-snapshot")
        return _Resp()

    monkeypatch.setattr(_praxis, "_auth_headers", lambda: {})
    monkeypatch.setattr(_praxis.urllib.request, "urlopen", fake_urlopen)
    # a snapshot-bound read emits BOTH x-praxis-space and x-praxis-snapshot (never one without the other)
    _praxis.facts_by(category="check", space="team-app", snapshot="building-validation")
    assert captured["space"] == "team-app"
    assert captured["snapshot"] == "building-validation"
