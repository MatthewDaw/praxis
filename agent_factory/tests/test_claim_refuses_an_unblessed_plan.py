"""Claiming a ticket out of an UNBLESSED plan is refused at the client.

THE INCIDENT (farming_analysis, 2026-08-09). A forgotten ``af-ticket-loop`` was still running while
a human was mid-intake on the very plan it was pointed at. It claimed tickets out of a plan that had
never been blessed, built them, and left branches that then poisoned the next run's orphan sweep.
Nothing anywhere refused the claim: the bless guard (S12) protects plan CONTENT from being edited
behind the plan's back, and claiming deliberately routes around it because taking a ticket is build
state, not plan content. So the one operation that turns an unfinished plan into running code was
the one operation with no bless check at all.

The guard lives on ``_praxis.claim_requirement`` — the choke point every builder goes through —
rather than only in the loop driver, because a driver check can be launched around (a bare af-build,
a hand-run worker, a second loop) and this cannot.

Two states are refused, and they are NOT the same refusal:
  ARMED          an intake session holds the planning marker RIGHT NOW; the tickets are moving.
  NEVER BLESSED  no ``blessed_at`` has ever been stamped; intake never finished.
A STALE armed marker is a dead session, so it does not arm — but a plan whose only marker is a stale
arm has still never been blessed, and stays refused for that reason.
"""

from __future__ import annotations

import time

import pytest
from hooks import _praxis

PROJECT = "demo"
SNAP = f"prd-{PROJECT}"
TICKET = "fact-1"


@pytest.fixture(autouse=True)
def _clear_cache():
    """The marker read is cached for a minute; a test must never inherit another test's plan."""
    _praxis._BLESS_CACHE.clear()
    yield
    _praxis._BLESS_CACHE.clear()


def _install(monkeypatch, marker_meta: dict | None) -> list[dict]:
    """Point ``facts_by`` at one planning marker (or none) and record every claim POST."""
    posted: list[dict] = []

    def facts_by(category=None, meta=None, state="active", space=None, snapshot=None):
        assert (space, snapshot) == (PROJECT, SNAP), "the marker must be read in the plan's own space"
        if category != _praxis.PLANNING_MARKER_CATEGORY or marker_meta is None:
            return []
        return [{"id": "marker-1", "scope": PROJECT, "meta": dict(marker_meta)}]

    def request(method, path, *, body=None, space=None, snapshot=None, **kw):
        posted.append({"path": path, "body": body})
        return {"claim": {"claim_owner": (body or {}).get("owner")}}

    monkeypatch.setattr(_praxis, "facts_by", facts_by)
    monkeypatch.setattr(_praxis, "_request", request)
    return posted


def _claim():
    return _praxis.claim_requirement(TICKET, "worker-a", 900, space=PROJECT, snapshot=SNAP)


def test_a_blessed_plan_claims_normally(monkeypatch):
    """The control. Everything below is worthless if the gate also blocks the happy path."""
    posted = _install(monkeypatch, {"planning_owner": None, "blessed_at": time.time()})

    assert _claim() == {"claim_owner": "worker-a"}
    assert [p["path"] for p in posted] == [f"/requirements/{TICKET}/claim"]


def test_an_armed_plan_refuses_the_claim(monkeypatch):
    """An intake session holds the marker: the ticket being claimed may not survive the edit."""
    posted = _install(monkeypatch, {"planning_owner": "sess-intake", "planning_at": time.time(),
                                    "blessed_at": time.time() - 5000})

    with pytest.raises(_praxis.PlanNotBlessed) as exc:
        _claim()

    assert "armed for editing" in str(exc.value)
    assert SNAP in str(exc.value)
    assert posted == [], "the claim must be refused BEFORE the request, not unwound after it"


def test_a_never_blessed_plan_refuses_the_claim(monkeypatch):
    """The farming_analysis case exactly: intake in progress, nothing ever blessed."""
    _install(monkeypatch, {"planning_owner": "sess-intake", "planning_at": time.time()})
    with pytest.raises(_praxis.PlanNotBlessed):
        _claim()

    _praxis._BLESS_CACHE.clear()
    # ...and with no marker fact at all, which is what a greenfield project looks like.
    posted = _install(monkeypatch, None)
    with pytest.raises(_praxis.PlanNotBlessed) as exc:
        _claim()
    assert "never blessed" in str(exc.value)
    assert posted == []


def test_a_stale_arm_does_not_arm_but_still_is_not_blessed(monkeypatch):
    """A crashed intake session must not arm the plan forever — but the absence of a live arm is not
    a bless either, and treating it as one would reopen the hole from the other side."""
    stale = {"planning_owner": "sess-dead",
             "planning_at": time.time() - (_praxis._PLANNING_TTL_S + 60)}

    _install(monkeypatch, stale)
    assert _praxis.plan_bless_state(PROJECT, SNAP, force=True) == "never-blessed"

    _praxis._BLESS_CACHE.clear()
    _install(monkeypatch, {**stale, "blessed_at": time.time() - 90_000})
    assert _praxis.plan_bless_state(PROJECT, SNAP, force=True) == "blessed", (
        "a stale arm over a plan that WAS blessed leaves it blessed — the marker's owner is dead"
    )


def test_refusal_is_typed_so_a_driver_can_tell_it_from_an_outage(monkeypatch):
    """`PlanNotBlessed` must not be catchable as `PraxisUnreachable`: "finish intake" and "the
    backend is down" demand opposite responses, and a driver that conflates them either spins
    against a healthy backend or gives up on a fixable one."""
    _install(monkeypatch, None)
    assert not issubclass(_praxis.PlanNotBlessed, _praxis.PraxisUnreachable)
    with pytest.raises(RuntimeError):  # it is still a RuntimeError, so nothing fails open
        _claim()


def test_a_claim_that_is_not_against_a_plan_snapshot_is_unaffected(monkeypatch):
    """Working memory and the checks snapshots have no planning marker and no bless semantics; the
    gate must be a no-op there rather than refusing every non-plan claim."""
    posted = _install(monkeypatch, None)

    assert _praxis.claim_requirement(TICKET, "worker-a", 900) == {"claim_owner": "worker-a"}
    assert _praxis.claim_requirement(TICKET, "worker-a", 900, space=PROJECT,
                                     snapshot="building-validation") == {"claim_owner": "worker-a"}
    assert len(posted) == 2


def test_the_marker_read_is_cached_so_a_round_pays_for_it_once(monkeypatch):
    """A round claims many tickets and the marker changes only at intake boundaries. One read per
    ticket would be a request per ticket for an answer that cannot have moved."""
    reads = []
    posted = _install(monkeypatch, {"planning_owner": None, "blessed_at": time.time()})
    real = _praxis.facts_by

    def counting(*a, **kw):
        reads.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(_praxis, "facts_by", counting)
    for _ in range(5):
        _claim()

    assert len(reads) == 1
    assert len(posted) == 5
