"""Locks the checks-resolution seam in ``hooks/_ticket_state.py`` + ``hooks/_praxis.py``:

Under the org -> space -> snapshot tenancy model every project IS a space (``space == <project>``).
Check RESOLUTION reads validation rules from a dedicated SNAPSHOT inside that project space, while
ticket STATE lives in the project's own ``prd-<project>`` snapshot. Snapshot defaults are per-scope
(validation -> ``building-validation``, planning -> ``planning-validation``); the skills override
per-invocation via ``checks_ref=`` — a bare snapshot name (space stays ``<project>``) or an explicit
``(space, snapshot)`` pair.

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


# --------------------------------------------------------------------------- the default mapping

def test_default_checks_snapshot_mapping():
    assert ts.default_checks_snapshot("validation") == "building-validation"
    assert ts.default_checks_snapshot("planning") == "planning-validation"
    assert ts.default_checks_snapshot(None) is None       # back-compat: no snapshot-bound reference
    assert ts.default_checks_snapshot("other") is None


# --------------------------------------------------------------------------- planning lane (af-intake)

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

def test_explicit_checks_ref_snapshot_overrides_default(monkeypatch):
    spy = _spy(monkeypatch, checks=[{"id": "L1", "scope": "planning"}])
    ts.resolve_validation_requirements({"id": "plan", "meta": {}}, project="team-app",
                                       scope="planning", checks_ref="my-lenses")
    # a bare snapshot override keeps space=<project>, swaps the snapshot
    assert spy.facts_by_spaces == ["team-app"]
    assert spy.facts_by_snapshots == ["my-lenses"]


def test_explicit_checks_ref_pair_overrides_space_and_snapshot(monkeypatch):
    spy = _spy(monkeypatch, checks=[{"id": "L1", "scope": "planning"}])
    ts.resolve_validation_requirements({"id": "plan", "meta": {}}, project="team-app",
                                       scope="planning",
                                       checks_ref=("shared-lenses", "planning-validation"))
    # an explicit (space, snapshot) pair overrides BOTH halves
    assert spy.facts_by_spaces == ["shared-lenses"]
    assert spy.facts_by_snapshots == ["planning-validation"]


def test_explicit_none_forces_default_reference(monkeypatch):
    spy = _spy(monkeypatch, checks=[{"id": "C1", "scope": "validation",
                                     "meta": {"applies_to": ["auth"]}}])
    ticket = {"id": "R1", "meta": {"tags": ["auth"]}}
    ts.resolve_validation_requirements(ticket, project="team-app", scope="validation",
                                       checks_ref=None)
    # None override => no snapshot-bound reference at all (neither header emitted; fail-closed safe)
    assert spy.facts_by_spaces and all(s is None for s in spy.facts_by_spaces)
    assert spy.facts_by_snapshots and all(s is None for s in spy.facts_by_snapshots)


def test_scope_none_backcompat_reads_default_reference(monkeypatch):
    spy = _spy(monkeypatch, checks=[{"id": "C1", "meta": {"applies_to": ["auth"]}}])
    ticket = {"id": "R1", "meta": {"tags": ["auth"]}}
    ts.resolve_validation_requirements(ticket, project="team-app")  # scope=None
    # scope=None has no default snapshot => no snapshot-bound reference (both halves None)
    assert spy.facts_by_spaces and all(s is None for s in spy.facts_by_spaces)
    assert spy.facts_by_snapshots and all(s is None for s in spy.facts_by_snapshots)


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
