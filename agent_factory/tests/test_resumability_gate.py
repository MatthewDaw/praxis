"""U2 — the claim-time resumability guard in ``hooks/_ticket_state.py:start_ticket``.

Before leasing a ticket, ``start_ticket`` runs the structural resumability probe over the ticket's meta
and its just-resolved required set. A NON-resumable ticket is NOT claimed: it is routed to an explicit
``under_specified: [missing fields]`` state (surfaced to intake) and ``start_ticket`` returns ``None``
without ever stamping ``build_state=in_progress``. A resumable ticket claims and pins exactly as before
— including the acceptance-less-but-check-covered case, which MUST NOT route back.

The guard applies the factory's own ``verify`` default ("automated" when absent, as start_ticket
already does) before probing, and does not evaluate dangling deps at claim time (a FINISHED prereq has
already left the live incomplete set), so it routes PRECISELY on the coverability contract — the same
empty-contract condition ``contract_with_floor`` would otherwise force the worker to ``block()`` on.

FakePraxis persists one ticket's meta (get_fact/patch_meta MERGE); facts_by/surface_checks are stubbed
so ONLY the acceptance floor (if any) resolves.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402


class FakePraxis:
    def __init__(self, meta):
        self._meta = dict(meta)

    def get_fact(self, cid, *, space=None, snapshot=None):
        return {"id": cid, "meta": dict(self._meta)}

    def patch_meta(self, cid, meta_dict, *, space=None, snapshot=None):
        self._meta.update(meta_dict)
        return {"id": cid, "meta": dict(self._meta)}

    def facts_by(self, category=None, meta=None, state="active", space=None, snapshot=None, **k):
        return []

    def surface_checks(self, project, screen_id, scope=None, space=None, snapshot=None):
        return []


def _install(monkeypatch, meta):
    fake = FakePraxis(meta)
    monkeypatch.setattr(ts, "_praxis", fake)
    return fake


def test_underspecified_ticket_is_not_leased_and_surfaces_missing(monkeypatch):
    # No acceptance AND no checks resolve -> not coverable-from-state -> routed, not claimed.
    fake = _install(monkeypatch, {"requirement_id": "R1", "tags": [], "verify": "automated"})
    out = ts.start_ticket("R1", "owner-a", project="team-app")
    assert out is None                                       # no lease handed back
    assert fake._meta.get(ts.M_UNDER_SPECIFIED) == ["contract"]
    assert fake._meta.get(ts.M_BUILD_STATE) != "in_progress"  # never claimed
    assert ts.M_CLAIM_OWNER not in fake._meta or fake._meta.get(ts.M_CLAIM_OWNER) is None


def test_resumable_ticket_claims_and_pins_unchanged(monkeypatch):
    # Acceptance present + verify set -> resumable -> claims + pins byte-identically to today.
    fake = _install(monkeypatch, {"requirement_id": "R2", "tags": [],
                                  "acceptance": "returns 200", "verify": "automated"})
    reqs = ts.start_ticket("R2", "owner-a", project="team-app")
    assert reqs and any("acceptance" in ts._check_id(r) for r in reqs)
    assert fake._meta.get(ts.M_BUILD_STATE) == "in_progress"
    assert fake._meta.get(ts.M_CLAIM_OWNER) == "owner-a"
    assert "R2::acceptance" in fake._meta.get(ts.M_REQUIRED_VALIDATIONS)
    assert ts.M_UNDER_SPECIFIED not in fake._meta or not fake._meta.get(ts.M_UNDER_SPECIFIED)


def test_acceptance_less_but_check_covered_ticket_still_claims(monkeypatch):
    # The regression guard: NO acceptance, but a declared check resolves -> resumable -> must claim.
    fake = _install(monkeypatch, {"requirement_id": "R3", "tags": ["auth"], "verify": "automated"})
    monkeypatch.setattr(fake, "facts_by",
                        lambda *a, **k: [{"id": "CHK-auth", "scope": "validation",
                                          "meta": {"applies_to": "auth"}}])
    reqs = ts.start_ticket("R3", "owner-a", project="team-app")
    assert reqs, "check-covered ticket must claim + return a non-empty contract"
    assert fake._meta.get(ts.M_BUILD_STATE) == "in_progress"
    assert "CHK-auth" in fake._meta.get(ts.M_REQUIRED_VALIDATIONS)


def test_absent_verify_defaults_to_automated_and_does_not_route(monkeypatch):
    # A ticket that omits verify is "automated" by factory convention (start_ticket already defaults
    # it), so it is NOT under-specified as long as it is coverable-from-state.
    fake = _install(monkeypatch, {"requirement_id": "R4", "tags": [], "acceptance": "it works"})
    reqs = ts.start_ticket("R4", "owner-a", project="team-app")
    assert reqs
    assert fake._meta.get(ts.M_BUILD_STATE) == "in_progress"
    assert ts.M_UNDER_SPECIFIED not in fake._meta or not fake._meta.get(ts.M_UNDER_SPECIFIED)


def test_adding_a_contract_clears_the_under_specified_marker(monkeypatch):
    # Integration: a ticket missing both acceptance and checks never enters the build set; adding an
    # acceptance condition makes the NEXT start_ticket claimable and clears the marker.
    fake = _install(monkeypatch, {"requirement_id": "R5", "tags": [], "verify": "automated"})
    assert ts.start_ticket("R5", "owner-a", project="team-app") is None
    assert fake._meta.get(ts.M_UNDER_SPECIFIED) == ["contract"]

    fake._meta["acceptance"] = "now it has a target"          # intake fixes the gap
    reqs = ts.start_ticket("R5", "owner-a", project="team-app")
    assert reqs
    assert fake._meta.get(ts.M_BUILD_STATE) == "in_progress"
    assert not fake._meta.get(ts.M_UNDER_SPECIFIED)           # marker cleared on the successful claim
