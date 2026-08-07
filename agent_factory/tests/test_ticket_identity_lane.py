"""Locks the R11/R12/R13 ticket-identity lane in ``hooks/_ticket_state.py`` (FL6, KD4):

  * a check bound directly to a ticket's own id (``meta.applies_to`` containing the ticket id
    literally — ingestion's narrowest-scope default, R12) is a MANDATORY, UNSKIPPABLE resolve lane:
    it pins on every claim/re-claim and survives diff-scoping (:func:`scope_checks_to_changes`) that
    would otherwise skip it (R11);
  * it survives even on a ticket that opts out of the SEPARATE universal lane (``universal_exempt``
    and/or every touched path sitting inside an exempt directory) — the identity lane is independent
    of that exemption machinery;
  * once the check's identity-bound ticket is gone (finished/deleted — nothing resolves it by id any
    more), the SAME check stays discoverable for any OTHER ticket via its observed-surface binding
    (R13) — the "afterlife conversion" is simply that this lane never depended on the dead id.

Fake ``_praxis`` (no network): ``facts_by`` mirrors the server's array-membership match on ANY meta
key (not just ``applies_to``), so both the identity lane and the surface-field lane are exercised.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402


class _DBSpy:
    """In-memory checks DB: array-membership match on every key of an ``meta`` query filter."""

    def __init__(self, checks=None):
        self._checks = checks or []

    def facts_by(self, category=None, meta=None, state="active", space=None, snapshot=None):
        meta = meta or {}
        out = []
        for c in self._checks:
            cmeta = c.get("meta") or {}
            if all(v in (cmeta.get(k) or []) for k, v in meta.items()):
                out.append(c)
        return out

    def surface_checks(self, project, screen_id, scope=None, space=None, snapshot=None):
        return []  # no renders-edge binding in any of these fixtures — surface-field lane only

    def context(self, query, top_k=10, as_of=None, space=None, snapshot=None):
        return []

    def get_fact(self, cid, space=None, snapshot=None, not_found_ok=False):
        return {"id": cid, "text": cid, "meta": {}}


def _check(cid, applies_to, run="pytest tests/test_x.py -q", surfaces=None, scope="validation"):
    meta = {"applies_to": applies_to, "scope": scope, "run": run}
    if surfaces is not None:
        meta["surfaces"] = surfaces
    return {"id": cid, "category": "check", "scope": scope, "meta": meta}


def _install(monkeypatch, checks):
    monkeypatch.setattr(ts, "_praxis", _DBSpy(checks=checks))


# --------------------------------------------------------------------------- R11: mandatory, unskippable

def test_identity_bound_check_resolves_only_for_its_own_ticket(monkeypatch):
    _install(monkeypatch, [_check("id-check", ["T1"])])
    mine = ts.resolve_validation_requirements({"id": "T1", "meta": {"tags": []}}, project="p")
    other = ts.resolve_validation_requirements({"id": "T2", "meta": {"tags": []}}, project="p")
    assert [c["id"] for c in mine] == ["id-check"]
    assert mine[0]["meta"]["identity_lane"] is True
    assert other == []


def test_identity_bound_check_pins_again_on_reclaim(monkeypatch):
    # A fresh resolve() is exactly what start_ticket runs on every claim AND re-claim — there is no
    # separate "first claim" state to fall out of sync, so calling it twice IS the re-claim proof.
    _install(monkeypatch, [_check("id-check", ["T1"])])
    ticket = {"id": "T1", "meta": {"tags": []}}
    first = ts.resolve_validation_requirements(ticket, project="p")
    second = ts.resolve_validation_requirements(ticket, project="p")
    assert [c["id"] for c in first] == ["id-check"]
    assert [c["id"] for c in second] == ["id-check"]


def test_identity_bound_check_is_never_skipped_by_diff_scoping():
    # Without the R11 exemption, a check scoped to "id_module" would be SKIPPED here: the diff never
    # touches id_module, and id_module IS a known root (so rule 3's "unknown blast radius" escape
    # hatch does not fire either) — the identity marker is the only thing keeping it in `to_run`.
    identity_chk = {"id": "id-check",
                    "meta": {"run": "cd id_module && pytest -q", "identity_lane": True}}
    other_chk = {"id": "other-check", "meta": {"run": "cd other_module && pytest -q"}}
    to_run, skipped = ts.scope_checks_to_changes([identity_chk, other_chk], ["other_module/file.py"])
    assert [c["id"] for c in to_run] == ["id-check", "other-check"]
    assert skipped == []


def test_identity_marker_survives_duplicate_run_collapsing():
    # An identity-bound check happens to share its run command with a plain lane-scoped one; the
    # collapse must not silently drop the unskippable marker onto a non-identity survivor's absence.
    identity_chk = {"id": "id-check",
                    "meta": {"applies_to": ["T1"], "run": "pytest tests/test_x.py -q",
                             "identity_lane": True}}
    lane_chk = {"id": "lane-check", "meta": {"applies_to": ["backend"], "run": "pytest tests/test_x.py -q"}}
    got = ts.collapse_duplicate_runs([lane_chk, identity_chk])
    assert len(got) == 1
    assert got[0]["meta"]["identity_lane"] is True
    assert got[0]["id"] == "id-check"  # the identity-bound entry itself is preferred as the survivor


# --------------------------------------------------------------------------- exempt from universal lane

def test_universal_exempt_and_path_exempt_ticket_still_carries_identity_lane_check(monkeypatch):
    _install(monkeypatch, [_check("id-check", ["T1"])])
    ticket_meta = {"tags": [], "universal_exempt": True}
    ticket = {"id": "T1", "meta": ticket_meta}
    resolved = ts.resolve_validation_requirements(ticket, project="p")
    assert [c["id"] for c in resolved] == ["id-check"]

    # Compose the full coverage contract exactly as start_ticket does — no acceptance text, an
    # explicit universal_exempt flag, AND every touched path inside an exempt directory.
    contract = ts.contract_with_floor("T1", "", resolved, ticket_meta=ticket_meta,
                                      paths=["docs/readme.md"])
    assert [c["id"] for c in contract] == ["id-check"]  # identity check present; no universal injected


# --------------------------------------------------------------------------- R13: afterlife conversion

def test_check_resolves_via_surface_binding_after_its_bound_ticket_is_gone(monkeypatch):
    # "T-old" is the ticket the check was originally identity-bound to at ingestion (R12); nothing
    # in this fixture claims T-old again (finished/deleted — its id is simply never resolved for).
    # A DIFFERENT ticket rendering the same observed surface must still discover the check.
    _install(monkeypatch, [_check("id-check", ["T-old"], surfaces=["s-login"])])
    new_ticket = {"id": "T-new", "meta": {"tags": [], "surfaces": ["s-login"]}}
    resolved = ts.resolve_validation_requirements(new_ticket, project="p")
    assert [c["id"] for c in resolved] == ["id-check"]
    # Found via the surface lane, NOT the (inapplicable, T-old-only) identity lane.
    assert "identity_lane" not in resolved[0]["meta"]


def test_a_ticket_rendering_a_different_surface_never_matches(monkeypatch):
    _install(monkeypatch, [_check("id-check", ["T-old"], surfaces=["s-login"])])
    unrelated = ts.resolve_validation_requirements(
        {"id": "T-new", "meta": {"tags": [], "surfaces": ["s-checkout"]}}, project="p")
    assert unrelated == []
