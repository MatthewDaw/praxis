"""BUG A — a kill_switched / suspended check must STOP gating, even on the R11 ticket-identity lane.

``resolve_validation_requirements`` pins a check whose ``meta.applies_to`` carries the ticket's OWN
id as MANDATORY and UNSKIPPABLE — "exempt from every exemption mechanism". But ``kill_switch`` (and
``suspend``) only stamp ``meta.kill_switch`` / an ``enforcement_state`` of suspended; the resolver
never read them, so a killed identity-bound check kept re-blocking its ticket every round forever (a
live 10-hour stall). The fix: a retired check (:func:`_ticket_state._is_retired`) is dropped from
EVERY lane, the identity lane included — an operator kill is a decision that the check itself is
stale, which is different from the diff-scoping / worker-discretion exemptions the unskippable clause
protects.

Uses the same in-memory ``facts_by`` double (array-membership on ``meta.applies_to``) as
``test_check_resolution_lanes.py`` — no network.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402

from agent_factory import ingestion_api  # noqa: E402


class _DBSpy:
    """Minimal checks DB: ``facts_by`` mimics the server's array-membership match on
    ``meta.applies_to`` (exactly the double in ``test_check_resolution_lanes.py``)."""

    def __init__(self, checks):
        self._checks = checks

    def facts_by(self, category=None, meta=None, state="active", space=None, snapshot=None):
        want = (meta or {}).get("applies_to")
        out = []
        for c in self._checks:
            applies = (c.get("meta") or {}).get("applies_to") or []
            if want is None or want in applies:
                out.append(c)
        return out

    def surface_checks(self, project, screen_id, scope=None, space=None, snapshot=None):
        return []


def _check(cid, applies_to, extra_meta=None):
    meta = {"applies_to": applies_to, "scope": "validation"}
    meta.update(extra_meta or {})
    return {"id": cid, "category": "check", "scope": "validation", "meta": meta}


def _resolve(monkeypatch, checks, ticket):
    monkeypatch.setattr(ts, "_praxis", _DBSpy(checks))
    got = ts.resolve_validation_requirements(ticket, project="p", scope="validation")
    return {c["id"] for c in got}


# --------------------------------------------------------------------------- control: it DOES pin

def test_a_live_identity_bound_check_pins(monkeypatch):
    ticket = {"id": "R1", "meta": {"tags": ["auth"]}}
    got = _resolve(monkeypatch, [_check("phantom", ["R1"]), _check("floor", ["*"])], ticket)
    assert got == {"phantom", "floor"}, "a live identity-bound check is mandatory and pins"


# --------------------------------------------------------------------------- the fix: retired => dropped

def test_a_kill_switched_identity_bound_check_no_longer_pins(monkeypatch):
    ticket = {"id": "R1", "meta": {"tags": ["auth"]}}
    checks = [_check("phantom", ["R1"], {"kill_switch": True, "kill_switch_reason": "stale"}),
              _check("floor", ["*"])]
    got = _resolve(monkeypatch, checks, ticket)
    assert "phantom" not in got, "a kill_switched check must not pin, even on the identity lane"
    assert got == {"floor"}, "the rest of the contract is unaffected"


def test_a_suspended_identity_bound_check_no_longer_pins(monkeypatch):
    ticket = {"id": "R1", "meta": {"tags": ["auth"]}}
    checks = [_check("phantom", ["R1"], {ts.M_ENFORCEMENT_STATE: ts.STATE_SUSPENDED}),
              _check("floor", ["*"])]
    got = _resolve(monkeypatch, checks, ticket)
    assert got == {"floor"}, "a suspended identity-bound check must not gate its ticket"


def test_an_archived_check_is_dropped_from_the_tag_lane_too(monkeypatch):
    # Retirement drops a check from EVERY lane, not only identity — a suspended tag-lane check is
    # equally not a live gate.
    ticket = {"id": "R2", "meta": {"tags": ["auth"]}}
    checks = [_check("auth-e2e", ["auth"], {ts.M_ENFORCEMENT_STATE: ts.STATE_ARCHIVED}),
              _check("floor", ["*"])]
    got = _resolve(monkeypatch, checks, ticket)
    assert got == {"floor"}


# --------------------------------------------------------------------------- constant-mirror guard

def test_enforcement_state_constants_mirror_ingestion_api():
    """``_ticket_state`` duplicates the enforcement-state values (ingestion_api imports it, so it
    can't import back). Lock the mirror so the two can never drift silently."""
    assert ts.M_ENFORCEMENT_STATE == ingestion_api.M_ENFORCEMENT_STATE
    assert ts.STATE_SUSPENDED == ingestion_api.STATE_SUSPENDED
    assert ts.STATE_ARCHIVED == ingestion_api.STATE_ARCHIVED
