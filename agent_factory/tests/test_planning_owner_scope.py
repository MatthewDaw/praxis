"""Validation script for ticket 961db89fbe3a463fbedcc6bf96328b1b:
planning_active is scoped to the owner that stamped the live planning marker.
"""
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_HOOKS = str(_HERE.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402
from _fake_praxis import SanctionedWrites  # noqa: E402


class FakePraxis(SanctionedWrites):
    """In-memory Praxis mirror for offline testing."""
    def __init__(self):
        self._facts = {}
        self._scopes = {}
        self.writes = []
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
                return cid
        self._seq += 1
        cid = f"generated-marker-{self._seq}"
        self._scopes[cid] = project
        self._facts.setdefault(cid, {})
        return cid


def _install():
    fake = FakePraxis()
    ts._praxis = fake
    return fake


def test_planning_active_scoped_to_owner():
    """A live planning marker stamped by owner-A does not arm for owner-B."""
    _install()
    ts.stamp_planning("team-app", "owner-A")
    assert ts.planning_active("team-app", owner="owner-B") is False, (
        "FAIL: planning_active should be False for owner-B when marker is owned by owner-A"
    )
    assert ts.planning_active("team-app", owner="owner-A") is True, (
        "FAIL: planning_active should be True for owner-A (the marker owner)"
    )
    assert ts.planning_active("team-app") is True, (
        "FAIL: planning_active without owner should still see any live marker"
    )
    print("PASS: planning_active scoped to owner")
    return True


def test_planning_active_backward_compat():
    """Calling planning_active without an owner keeps existing behavior."""
    _install()
    assert ts.planning_active("team-app") is False
    ts.stamp_planning("team-app", "owner-A")
    assert ts.planning_active("team-app") is True
    print("PASS: planning_active backward compatible without owner arg")
    return True


def test_clear_planning_reclaims_stale_marker():
    """A marker whose owner is NOT LIVE (stale) is reclaimable by a different owner."""
    fake = _install()
    mid = ts.stamp_planning("team-app", "owner-A")
    fake._facts[mid][ts.M_PLANNING_AT] = time.time() - ts.DEFAULT_PLANNING_TTL_S - 5
    assert ts.planning_live(fake._facts[mid]) is False, (
        "FAIL: marker should be stale after aging past TTL"
    )
    result = ts.clear_planning("team-app", "owner-B")
    assert result is True, (
        f"FAIL: clear_planning should allow reclaim of stale marker, got {result}"
    )
    print("PASS: clear_planning reclaims stale marker for different owner")
    return True


def test_clear_planning_rejects_live_other_owner():
    """A LIVE marker owned by owner-A cannot be cleared by owner-B."""
    _install()
    ts.stamp_planning("team-app", "owner-A")
    result = ts.clear_planning("team-app", "owner-B")
    assert result is False, (
        f"FAIL: clear_planning should reject live marker from different owner, got {result}"
    )
    result2 = ts.clear_planning("team-app", "owner-A")
    assert result2 is True, (
        f"FAIL: clear_planning should allow owner to clear their own marker, got {result2}"
    )
    print("PASS: clear_planning rejects live marker from different owner")
    return True


def test_planning_active_stale_returns_false():
    """A stale marker returns False for any owner (including original)."""
    fake = _install()
    mid = ts.stamp_planning("team-app", "owner-A")
    fake._facts[mid][ts.M_PLANNING_AT] = time.time() - ts.DEFAULT_PLANNING_TTL_S - 5
    assert ts.planning_active("team-app", owner="owner-A") is False, (
        "FAIL: stale marker should return False even for original owner"
    )
    assert ts.planning_active("team-app") is False, (
        "FAIL: stale marker should return False without owner too"
    )
    print("PASS: stale marker returns False")
    return True


if __name__ == "__main__":
    failures = 0
    for test in [
        test_planning_active_scoped_to_owner,
        test_planning_active_backward_compat,
        test_clear_planning_reclaims_stale_marker,
        test_clear_planning_rejects_live_other_owner,
        test_planning_active_stale_returns_false,
    ]:
        try:
            if not test():
                failures += 1
        except AssertionError as e:
            print(f"  {e}")
            failures += 1
        except Exception as e:
            print(f"  ERROR in {test.__name__}: {e}")
            failures += 1

    if failures:
        print(f"\n{failures} TEST(S) FAILED")
        sys.exit(1)
    else:
        print("\nALL TESTS PASSED")
        sys.exit(0)
