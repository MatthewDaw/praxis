"""U7 — pool candidates and gating checks SURVIVE ticket-start (re-resolved, not deleted).

The R3 claim this locks: ticket-start truncation clears only ``pinned_checks`` (the worker's
per-pass synthesized eval); it never removes a check from the ``building-validation`` store, and
resolution is a stateless query. So a ``candidate:true`` pool entry and a ``candidate:false`` gate
both re-resolve to their lanes on every pass — the two-source pool is durable across builds.

Characterized at the resolve + ``pin_requirements`` layer (the only truncation site); ``start_ticket``
is a thin wrapper (claim -> resolve -> contract_with_floor -> pin_requirements).
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402


class _DBSpy:
    def __init__(self, checks):
        self._checks = checks
        self.patches = []

    def facts_by(self, category=None, meta=None, state="active", space=None, snapshot=None):
        want = (meta or {}).get("applies_to")
        return [c for c in self._checks
                if want is None or want in ((c.get("meta") or {}).get("applies_to") or [])]

    def surface_checks(self, project, screen_id, scope=None, space=None, snapshot=None):
        return []

    def patch_meta(self, cid, patch, **kw):
        # Records the write; crucially does NOT touch the checks store (mirrors the server: pinning
        # writes ticket meta, never deletes building-validation facts).
        self.patches.append((cid, patch))
        return {"id": cid, "meta": patch}


def _check(cid, applies_to, candidate=None):
    meta = {"applies_to": applies_to, "scope": "validation"}
    if candidate is not None:
        meta["candidate"] = candidate
    return {"id": cid, "category": "check", "scope": "validation", "meta": meta}


def _install(monkeypatch, checks):
    spy = _DBSpy(checks)
    monkeypatch.setattr(ts, "_praxis", spy)
    return spy


CHECKS = [
    _check("gate", ["auth"], candidate=False),
    _check("suggestion", ["auth"], candidate=True),
    _check("floor", ["*"], candidate=False),
]
TICKET = {"id": "R1", "meta": {"tags": ["auth"]}}


def test_resolution_is_stateless_across_passes(monkeypatch):
    _install(monkeypatch, list(CHECKS))
    gate1 = {c["id"] for c in ts.resolve_validation_requirements(TICKET, project="p", scope="validation")}
    pool1 = {c["id"] for c in ts.pool_candidates(TICKET, project="p", scope="validation")}
    # A second pass (as af-build re-does every iteration) returns the identical lanes.
    gate2 = {c["id"] for c in ts.resolve_validation_requirements(TICKET, project="p", scope="validation")}
    pool2 = {c["id"] for c in ts.pool_candidates(TICKET, project="p", scope="validation")}
    assert gate1 == gate2 == {"gate", "floor"}
    assert pool1 == pool2 == {"suggestion"}


def test_pin_requirements_truncates_only_pinned_checks(monkeypatch):
    spy = _install(monkeypatch, list(CHECKS))
    gating = ts.resolve_validation_requirements(TICKET, project="p", scope="validation")
    ts.pin_requirements("R1", gating)
    _cid, patch = spy.patches[-1]
    assert patch[ts.M_PINNED_CHECKS] == []                       # per-pass eval cleared...
    assert set(patch[ts.M_REQUIRED_VALIDATIONS]) == {"gate", "floor"}  # ...contract re-derived from the pool


def test_pool_and_gate_survive_a_pin(monkeypatch):
    _install(monkeypatch, list(CHECKS))
    gating = ts.resolve_validation_requirements(TICKET, project="p", scope="validation")
    ts.pin_requirements("R1", gating)                            # simulate ticket start truncation
    # After pinning, both lanes STILL resolve — pinning never deleted a building-validation fact.
    assert {c["id"] for c in ts.resolve_validation_requirements(TICKET, project="p", scope="validation")} \
        == {"gate", "floor"}
    assert {c["id"] for c in ts.pool_candidates(TICKET, project="p", scope="validation")} == {"suggestion"}
