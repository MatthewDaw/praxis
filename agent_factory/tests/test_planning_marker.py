"""Locks U5: the planning-session arming marker (``stamp_planning`` / ``clear_planning`` /
``planning_active``) on ``hooks/_ticket_state.py``.

Mirror of the whole-set run marker, but for the plan hook: intake stamps at start, clears at bless;
``planning_active`` is True while a non-stale marker is present. The marker fact lives in the plan
snapshot with a SERVER-GENERATED id, resolved by its idempotency key (scope=project,
category="planning-marker") the same way a surface is — ``ensure_planning_marker`` bootstraps it
find-or-create on the server, so a greenfield project can stamp at all and two concurrent intakes
cannot mint two markers. Every read is NOT-FOUND-TOLERANT (no marker == inactive, not a hard error).
"""

import sys
import time
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402


class FakePraxis:
    """A tiny in-memory Praxis: get_fact reads a fact's stored meta (404-tolerant), patch_meta merges.
    Records the (space, snapshot) each write bound to so binding can be asserted.

    ``ensure_planning_marker`` models the SERVER-side find-or-create: the marker id is generated
    (not a computed address) and is idempotent per project, so calling it twice returns the same id.
    ``facts_by`` backs the read path that resolves an existing marker by its idempotency key."""

    def __init__(self):
        self._facts = {}          # cid -> meta dict
        self._scopes = {}         # cid -> scope (the bare project)
        self.writes = []          # (cid, patch, space, snapshot)
        self._seq = 0

    def get_fact(self, cid, *, space=None, snapshot=None, not_found_ok=False):
        if cid not in self._facts:
            if not_found_ok:
                return {}
            raise ts.PraxisUnreachable(f"404 {cid}")
        return {"id": cid, "meta": dict(self._facts[cid])}

    def patch_meta(self, cid, meta_dict, *, space=None, snapshot=None):
        self.writes.append((cid, meta_dict, space, snapshot))
        meta = self._facts.setdefault(cid, {})
        for k, v in meta_dict.items():
            if v is None:
                meta.pop(k, None)
            else:
                meta[k] = v
        return {"id": cid, "meta": dict(meta)}

    def facts_by(self, category=None, meta=None, *, space=None, snapshot=None):
        if category != ts.PLANNING_MARKER_CATEGORY:
            return []
        return [{"id": cid, "scope": scope, "meta": dict(self._facts.get(cid) or {})}
                for cid, scope in self._scopes.items()]

    def ensure_planning_marker(self, project, *, space=None, snapshot=None):
        for cid, scope in self._scopes.items():
            if scope == project:
                return cid                      # idempotent: one marker per project
        self._seq += 1
        cid = f"generated-marker-{self._seq}"
        self._scopes[cid] = project
        self._facts.setdefault(cid, {})
        return cid


def _install(monkeypatch):
    fake = FakePraxis()
    monkeypatch.setattr(ts, "_praxis", fake)
    return fake


# --------------------------------------------------------------------------- id + binding

def test_planning_project_strips_prefix():
    assert ts.planning_project("team-app") == "team-app"
    assert ts.planning_project("prd-team-app") == "team-app"


def test_marker_id_is_empty_until_created(monkeypatch):
    """A pure read returns "" when nothing was ever stamped — the Stop hook needs absence to mean
    'inactive', never a fabricated address that 404s against the real backend."""
    _install(monkeypatch)
    assert ts.planning_marker_id("team-app") == ""


def test_marker_is_bootstrapped_and_idempotent(monkeypatch):
    """create=True materializes the marker on a greenfield project; a bare project and the snapshot
    name resolve to the SAME marker (one per project)."""
    _install(monkeypatch)
    mid = ts.planning_marker_id("team-app", create=True)
    assert mid                                             # a real, server-generated id
    assert ts.planning_marker_id("prd-team-app", create=True) == mid
    assert ts.planning_marker_id("team-app") == mid        # now resolvable by pure read


def test_stamp_binds_to_plan_snapshot(monkeypatch):
    fake = _install(monkeypatch)
    mid = ts.stamp_planning("team-app", "sess-A")
    assert mid == ts.planning_marker_id("team-app")        # stamped onto the resolved marker
    cid, patch, space, snap = fake.writes[-1]
    assert (cid, space, snap) == (mid, "team-app", "prd-team-app")
    assert patch[ts.M_PLANNING_OWNER] == "sess-A"


# --------------------------------------------------------------------------- active lifecycle

def test_stamp_then_active(monkeypatch):
    _install(monkeypatch)
    assert ts.planning_active("team-app") is False       # no marker yet
    ts.stamp_planning("team-app", "sess-A")
    assert ts.planning_active("team-app") is True


def test_clear_then_inactive(monkeypatch):
    _install(monkeypatch)
    ts.stamp_planning("team-app", "sess-A")
    assert ts.clear_planning("team-app", "sess-A") is True
    assert ts.planning_active("team-app") is False


def test_clear_rejects_other_owner(monkeypatch):
    _install(monkeypatch)
    ts.stamp_planning("team-app", "sess-A")
    assert ts.clear_planning("team-app", "sess-B") is False
    assert ts.planning_active("team-app") is True         # still armed for the real owner


def test_stale_marker_is_inactive(monkeypatch):
    fake = _install(monkeypatch)
    mid = ts.stamp_planning("team-app", "sess-A")
    # age the marker past its TTL.
    fake._facts[mid][ts.M_PLANNING_AT] = time.time() - ts.DEFAULT_PLANNING_TTL_S - 5
    assert ts.planning_active("team-app") is False


def test_clear_is_a_noop_when_never_stamped(monkeypatch):
    """Clearing a project that never stamped must not patch a non-existent fact — it is already
    inert, so report success rather than 404ing at bless time."""
    _install(monkeypatch)
    assert ts.clear_planning("never-planned", "sess-A") is True


def test_no_marker_is_inactive(monkeypatch):
    _install(monkeypatch)
    assert ts.planning_active("other-project") is False


# --------------------------------------------------------------------------- planning_live boundary

def test_planning_live_boundary():
    now = 1_000.0
    assert ts.planning_live({ts.M_PLANNING_OWNER: "s", ts.M_PLANNING_AT: now}, now=now) is True
    edge = now - ts.DEFAULT_PLANNING_TTL_S
    assert ts.planning_live({ts.M_PLANNING_OWNER: "s", ts.M_PLANNING_AT: edge}, now=now) is True
    stale = now - ts.DEFAULT_PLANNING_TTL_S - 1
    assert ts.planning_live({ts.M_PLANNING_OWNER: "s", ts.M_PLANNING_AT: stale}, now=now) is False
    assert ts.planning_live({ts.M_PLANNING_AT: now}, now=now) is False   # no owner
