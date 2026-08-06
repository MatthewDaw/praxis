"""The build loop's state writes must not go through ``patch_meta``.

``PATCH /candidates/{cid}`` is subject to the S12 bless guard, which refuses edits to a
blessed ``prd-<project>`` snapshot. A build only ever runs against a blessed plan, so
routing claim / pin / release through it meant none of them worked: workers could not
claim, the check pin was refused in silence (leaving ``pinned_checks: []``, which reads
afterwards as "RESOLVE never ran"), and finishes were refused.

The server-side half -- that the sanctioned routes write on a blessed snapshot while the
candidate-edit path is still refused -- is pinned in
``knowledge/knowledge_graph/tests/test_build_state_on_a_blessed_plan.py``. THIS file pins
the client half: that ``_ticket_state`` actually CALLS those routes. A double implementing
only ``get_fact``/``patch_meta`` must now fail loudly rather than quietly recording a write
the live server would have refused -- otherwise the whole suite could stay green while the
loop remained broken in production, which is exactly what happened.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _praxis  # noqa: E402
import _ticket_state as ts  # noqa: E402
from _fake_praxis import SanctionedWrites  # noqa: E402

PLAN = ("team-app", "prd-team-app")


class LegacyOnly:
    """A double that offers ONLY the guarded candidate-edit write."""

    def __init__(self, meta=None):
        self._meta = dict(meta or {})
        self.patches = []

    def get_fact(self, cid, *, space=None, snapshot=None):
        return {"id": cid, "meta": dict(self._meta)}

    def patch_meta(self, cid, meta_dict, *, space=None, snapshot=None):
        self.patches.append(dict(meta_dict))
        self._meta.update(meta_dict)
        return {"id": cid, "meta": dict(self._meta)}


class Sanctioned(SanctionedWrites, LegacyOnly):
    """The same storage, plus the sanctioned build-state routes."""


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: ts.claim("R1", "w", ref=PLAN), id="claim"),
        pytest.param(lambda: ts.pin_requirements("R1", [{"id": "c1"}], ref=PLAN), id="pin_requirements"),
        pytest.param(
            lambda: ts.pin_validations("R1", [{"validation_id": "v1", "covers": ["c1"],
                                               "run": "pytest"}], ref=PLAN),
            id="pin_validations",
        ),
        pytest.param(lambda: ts.record_validation_pass("R1", "v1", True, ref=PLAN), id="record_pass"),
        pytest.param(lambda: ts.block("R1", "w", "needs a secret", ref=PLAN), id="block"),
        pytest.param(lambda: ts.stamp_run(["R1"], "run-1", ref=PLAN), id="stamp_run"),
    ],
)
def test_build_state_writes_no_longer_reach_patch_meta(monkeypatch, call):
    """Each write must go to the sanctioned route -- a legacy-only double cannot serve it."""
    fake = LegacyOnly({"requirement_id": "R1"})
    monkeypatch.setattr(ts, "_praxis", fake)
    with pytest.raises(AttributeError):
        call()
    assert fake.patches == [], "nothing may have been written through the guarded path"


def test_release_no_longer_reaches_patch_meta(monkeypatch):
    fake = LegacyOnly({"requirement_id": "R1", ts.M_CLAIM_OWNER: "w"})
    monkeypatch.setattr(ts, "_praxis", fake)
    with pytest.raises(AttributeError):
        ts.release("R1", "w", "finished", ref=PLAN)
    assert fake.patches == []


def test_the_planning_marker_deliberately_still_uses_patch_meta(monkeypatch):
    """The ONE thing that must keep the guarded path.

    The planning marker is the bless guard's own CONTROL SURFACE -- ``stamp_planning``
    re-arms a blessed plan by mutating this very fact, and the guard exempts it by name so
    that recovery path is not deadlocked by itself. Moving it onto the build-state route
    would be moving the guard's own switch out from under it."""
    fake = LegacyOnly({})
    monkeypatch.setattr(ts, "_praxis", fake)
    monkeypatch.setattr(ts, "planning_marker_id", lambda project, create=False: "prd-x::planning")

    ts.stamp_planning("team-app", "intake-1")

    assert fake.patches and ts.M_PLANNING_OWNER in fake.patches[-1]


# --------------------------------------------------- and the sanctioned route does the work

def test_the_whole_ticket_lifecycle_runs_on_the_sanctioned_routes(monkeypatch):
    """Claim -> pin -> validate -> finish, end to end, with patch_meta unavailable for state."""
    fake = Sanctioned({"requirement_id": "R1"})
    monkeypatch.setattr(ts, "_praxis", fake)

    assert ts.claim("R1", "w", ref=PLAN) is True
    assert fake._meta[ts.M_BUILD_STATE] == "in_progress"
    assert fake._meta[ts.M_CLAIM_OWNER] == "w"

    ts.pin_requirements("R1", [{"id": "c1"}], ref=PLAN)
    assert fake._meta[ts.M_REQUIRED_VALIDATIONS] == ["c1"]
    assert fake._meta[ts.M_PINNED_CHECKS] == [], "a fresh contract truncates the prior eval"

    ts.pin_validations("R1", [{"validation_id": "v1", "covers": ["c1"], "run": "pytest"}], ref=PLAN)
    ts.record_validation_pass("R1", "v1", True, ref=PLAN)
    assert ts.all_validations_passed("R1", ref=PLAN) is True

    assert ts.release("R1", "w", "finished", ref=PLAN) is True
    assert fake._meta[ts.M_BUILD_STATE] == "finished"
    assert fake._meta.get(ts.M_CLAIM_OWNER) is None


def test_a_claim_lost_to_a_live_lease_is_false_not_an_outage(monkeypatch):
    """409 is a real answer -- move to the next ticket, do not fail the run closed."""
    import time

    fake = Sanctioned({
        ts.M_BUILD_STATE: "in_progress", ts.M_CLAIM_OWNER: "other",
        ts.M_CLAIM_HEARTBEAT_AT: time.time(), ts.M_CLAIM_LEASE_TTL: 900,
    })
    monkeypatch.setattr(ts, "_praxis", fake)
    assert ts.claim("R1", "w", ref=PLAN) is False


def test_a_conflict_is_still_an_unreachable_subclass_for_every_gate():
    """Gates catch PraxisUnreachable and fail closed; a lost lease must not slip past them."""
    assert issubclass(_praxis.PraxisConflict, _praxis.PraxisUnreachable)
