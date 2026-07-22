"""U1 — the ``meta.candidate`` discriminator in check resolution (`hooks/_ticket_state.py`).

The shared ``building-validation`` pool holds two kinds of check:
  * ``candidate:false`` / absent — a HARD GATE. Resolves into ``required_validations`` exactly as
    today (tag ∪ ``"*"`` ∪ surface), so ``candidate``-free plans behave byte-identically.
  * ``candidate:true`` — a NON-GATING pool entry. Excluded from the mandatory resolve; returned only
    by the deterministic ``pool_candidates`` query (the input the build-time assembler tiers).

Fake ``_praxis`` mirrors the server's ``applies_to`` array-membership filter, so both lanes are
asserted deterministically without a network.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402


class _DBSpy:
    """Minimal in-memory checks DB; ``facts_by`` mimics the server's ``applies_to`` membership match."""

    def __init__(self, checks=None):
        self._checks = checks or []

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


def _check(cid, applies_to, candidate=None, scope="validation"):
    meta = {"applies_to": applies_to, "scope": scope}
    if candidate is not None:
        meta["candidate"] = candidate
    return {"id": cid, "category": "check", "scope": scope, "meta": meta}


def _install(monkeypatch, checks):
    spy = _DBSpy(checks=checks)
    monkeypatch.setattr(ts, "_praxis", spy)
    return spy


def _gating(ticket):
    return {c["id"] for c in ts.resolve_validation_requirements(ticket, project="p", scope="validation")}


def _pool(ticket):
    return {c["id"] for c in ts.pool_candidates(ticket, project="p", scope="validation")}


# --------------------------------------------------------------------------- gating lane (unchanged)

def test_candidate_false_check_gates_like_today(monkeypatch):
    _install(monkeypatch, [_check("must-gate", ["auth"], candidate=False), _check("floor", ["*"])])
    ticket = {"id": "R1", "meta": {"tags": ["auth"]}}
    assert _gating(ticket) == {"must-gate", "floor"}


def test_candidate_absent_check_gates_like_today(monkeypatch):
    # No candidate field at all == the pre-U1 world. Must be byte-identical (regression guard).
    _install(monkeypatch, [_check("auth-e2e", ["auth"]), _check("floor", ["*"])])
    ticket = {"id": "R2", "meta": {"tags": ["auth"]}}
    assert _gating(ticket) == {"auth-e2e", "floor"}
    assert _pool(ticket) == set()  # nothing marked candidate -> empty pool


# --------------------------------------------------------------------------- candidate lane (new)

def test_candidate_true_excluded_from_gating_present_in_pool(monkeypatch):
    _install(monkeypatch, [
        _check("must-gate", ["auth"], candidate=False),
        _check("suggestion", ["auth"], candidate=True),
        _check("floor", ["*"]),
    ])
    ticket = {"id": "R3", "meta": {"tags": ["auth"]}}
    assert _gating(ticket) == {"must-gate", "floor"}   # candidate excluded from the gate
    assert _pool(ticket) == {"suggestion"}             # ...and present in the pool


def test_pool_candidates_is_deterministic_full_set(monkeypatch):
    # The assembler needs EVERY matching candidate, not a top-k sample.
    _install(monkeypatch, [
        _check("s1", ["auth"], candidate=True),
        _check("s2", ["auth"], candidate=True),
        _check("s3", ["*"], candidate=True),
    ])
    ticket = {"id": "R4", "meta": {"tags": ["auth"]}}
    assert _pool(ticket) == {"s1", "s2", "s3"}


def test_candidate_tag_does_not_bleed_onto_unrelated_ticket(monkeypatch):
    _install(monkeypatch, [_check("ui-suggestion", ["screen"], candidate=True), _check("floor", ["*"])])
    backend = {"id": "R5", "meta": {"tags": ["backend"]}}
    assert _pool(backend) == set()        # candidate scoped to "screen" never resolves onto backend
    assert _gating(backend) == {"floor"}
