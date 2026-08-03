"""Locks the lease-takeover semantics of ``release()`` and the implicit heartbeat
(``hooks/_ticket_state.py``).

THE BUG THIS LOCKS. A claim carries a lease (default 900s). Nothing bumped ``claim_heartbeat_at``
during BUILD, so a ticket that legitimately took longer than the lease window expired its own lease
*while its owner was still working*. Another agent then reclaimed and rebuilt it, and when the first
owner finished, ``release(..., "finished")`` hit the owner-mismatch guard and returned False WITHOUT
WRITING — silently discarding completed, check-passing work. The ticket stayed incomplete, was handed
out again, and the race repeated: an unbounded rebuild loop that burned an entire run's token budget
while finishing one ticket, invisible because no outcome was ever recorded.

The fix is asymmetric on purpose, and these tests pin that asymmetry:
  * ``finished``   is HONORED through a takeover (completion is a fact about the world) and warns.
  * ``incomplete`` still REFUSES through a takeover (a stale owner must not regress an active build).
Plus an implicit heartbeat on ``record_validation_pass``, so liveness never depends on the build agent
remembering to call :func:`heartbeat` on a timer.
"""

import importlib
import os
import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402
from _fake_praxis import SanctionedWrites  # noqa: E402

PLAN = ("team-app", "prd-team-app")


class FakePraxis(SanctionedWrites):
    """Persists one ticket's meta across get_fact/patch_meta (MERGE), like the live server's PATCH."""

    def __init__(self, meta=None):
        self._meta = dict(meta or {})
        self.writes = []

    def get_fact(self, cid, *, space=None, snapshot=None):
        return {"id": cid, "meta": dict(self._meta)}

    def patch_meta(self, cid, meta_dict, *, space=None, snapshot=None):
        self.writes.append(dict(meta_dict))
        self._meta.update(meta_dict)
        return {"id": cid, "meta": dict(self._meta)}


def _claimed_by(owner, **extra):
    meta = {
        ts.M_BUILD_STATE: "in_progress",
        ts.M_CLAIM_OWNER: owner, ts.M_CLAIM_AT: 123.0,
        ts.M_CLAIM_HEARTBEAT_AT: 123.0, ts.M_CLAIM_LEASE_TTL: 900,
        "requirement_id": "OD-13",
    }
    meta.update(extra)
    return meta


# --------------------------------------------------------------- release: finished through takeover

def test_finished_is_honored_when_the_lease_was_stolen(monkeypatch, capsys):
    """The regression: a finish must NOT be silently dropped because the lease was taken over."""
    fake = FakePraxis(_claimed_by("agent-B"))  # B stole the lease while A was still working
    monkeypatch.setattr(ts, "_praxis", fake)

    assert ts.release("R1", "agent-A", "finished", ref=PLAN) is True

    meta = fake.get_fact("R1")["meta"]
    assert meta[ts.M_BUILD_STATE] == "finished"     # the completion was RECORDED, not discarded
    assert fake.writes, "a takeover-finish must still write"
    # and it is LOUD — a silent takeover is what made the original bug undiagnosable
    err = capsys.readouterr().err
    assert "LEASE TAKEOVER" in err and "OD-13" in err


def test_finished_by_the_holder_is_silent(monkeypatch, capsys):
    fake = FakePraxis(_claimed_by("agent-A"))
    monkeypatch.setattr(ts, "_praxis", fake)

    assert ts.release("R1", "agent-A", "finished", ref=PLAN) is True
    assert fake.get_fact("R1")["meta"][ts.M_BUILD_STATE] == "finished"
    assert "LEASE" not in capsys.readouterr().err  # no warning on the normal path


def test_finished_on_an_unclaimed_ticket_still_works(monkeypatch, capsys):
    fake = FakePraxis({ts.M_BUILD_STATE: "in_progress", "requirement_id": "OD-13"})
    monkeypatch.setattr(ts, "_praxis", fake)

    assert ts.release("R1", "agent-A", "finished", ref=PLAN) is True
    assert "LEASE" not in capsys.readouterr().err


# ------------------------------------------------------------- release: incomplete keeps the guard

def test_incomplete_is_refused_when_the_lease_was_stolen(monkeypatch, capsys):
    """A yield is a claim about the CURRENT attempt — a stale owner must not regress an active build."""
    fake = FakePraxis(_claimed_by("agent-B"))
    monkeypatch.setattr(ts, "_praxis", fake)

    assert ts.release("R1", "agent-A", "incomplete", ref=PLAN) is False

    assert fake.writes == [], "a refused yield must not write"
    assert fake.get_fact("R1")["meta"][ts.M_BUILD_STATE] == "in_progress"  # B's build untouched
    err = capsys.readouterr().err
    assert "LEASE LOST" in err and "OD-13" in err  # refused, but never silent


def test_incomplete_by_the_holder_still_regresses(monkeypatch):
    fake = FakePraxis(_claimed_by("agent-A", **{ts.M_RUN_OWNER: "run", ts.M_RUN_AT: 1.0}))
    monkeypatch.setattr(ts, "_praxis", fake)

    assert ts.release("R1", "agent-A", "incomplete", ref=PLAN) is True
    meta = fake.get_fact("R1")["meta"]
    assert meta[ts.M_BUILD_STATE] == "incomplete"
    assert meta[ts.M_RUN_OWNER] == "run", "a clean yield keeps the run marker so the ticket is re-done"


# ------------------------------------------------------------------------- implicit heartbeat

def test_recording_a_validation_bumps_the_heartbeat(monkeypatch):
    """Recording a check result IS proof of liveness — so it must refresh the lease for free."""
    fake = FakePraxis(_claimed_by("agent-A", **{ts.M_CLAIM_HEARTBEAT_AT: 1.0}))
    monkeypatch.setattr(ts, "_praxis", fake)

    ts.record_validation_pass("R1", "v1", True, ref=PLAN)

    assert fake.get_fact("R1")["meta"][ts.M_CLAIM_HEARTBEAT_AT] > 1.0
    assert len(fake.writes) == 1, "the heartbeat must piggyback, not cost an extra round-trip"


def test_heartbeat_is_not_bumped_on_an_unclaimed_or_idle_ticket(monkeypatch):
    for meta in ({ts.M_BUILD_STATE: "in_progress"},                      # unclaimed
                 {ts.M_CLAIM_OWNER: "agent-A", ts.M_BUILD_STATE: "incomplete"}):  # not building
        fake = FakePraxis(dict(meta))
        monkeypatch.setattr(ts, "_praxis", fake)
        ts.record_validation_pass("R1", "v1", True, ref=PLAN)
        assert ts.M_CLAIM_HEARTBEAT_AT not in fake.writes[0]


# ------------------------------------------------------------------------- configurable lease TTL

def test_lease_ttl_is_env_overridable(monkeypatch):
    """Without this, widening a lease means editing the factory to make one project work."""
    monkeypatch.setenv("AF_LEASE_TTL_S", "5400")
    assert importlib.reload(ts).DEFAULT_LEASE_TTL_S == 5400
    monkeypatch.delenv("AF_LEASE_TTL_S", raising=False)
    assert importlib.reload(ts).DEFAULT_LEASE_TTL_S == 900


def test_a_bogus_ttl_override_falls_back_to_the_default(monkeypatch, capsys):
    for bad in ("garbage", "-5", "0"):
        monkeypatch.setenv("AF_LEASE_TTL_S", bad)
        assert importlib.reload(ts).DEFAULT_LEASE_TTL_S == 900, f"{bad!r} must not take effect"
        assert "WARNING" in capsys.readouterr().err
    monkeypatch.delenv("AF_LEASE_TTL_S", raising=False)
    importlib.reload(ts)


def teardown_module(_module):
    """Leave the module in its default state for tests that import it after this file."""
    os.environ.pop("AF_LEASE_TTL_S", None)
    importlib.reload(ts)
