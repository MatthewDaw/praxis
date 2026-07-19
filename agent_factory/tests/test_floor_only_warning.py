"""Locks item 5: the build-time floor-only belt in ``hooks/_ticket_state.py:start_ticket``.

When a ticket is ``verify="automated"`` but ``resolve_validation_requirements`` returns ZERO declared
checks (only its acceptance FLOOR covers it), start_ticket writes a loud ``[af-build] WARNING`` naming
the ticket to stderr — the gap (no declared validation gate) is surfaced even on an old plan. A
``verify="manual"`` ticket carries no such expectation, so it must NOT warn.

FakePraxis persists ONE ticket's meta (get_fact/patch_meta merge); facts_by/surface_checks are stubbed
empty so ONLY the acceptance floor resolves — the floor-only condition the belt fires on.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402


class FakePraxis:
    """Persists one ticket's meta across get_fact/patch_meta (MERGE), like the live server's PATCH."""

    def __init__(self, meta=None):
        self._meta = dict(meta or {})

    def get_fact(self, cid, *, space=None, snapshot=None):
        return {"id": cid, "meta": dict(self._meta)}

    def patch_meta(self, cid, meta_dict, *, space=None, snapshot=None):
        self._meta.update(meta_dict)
        return {"id": cid, "meta": dict(self._meta)}

    def facts_by(self, category=None, meta=None, state="active", space=None, snapshot=None, **k):
        return []  # NO declared checks resolve -> only the acceptance floor

    def surface_checks(self, project, screen_id, scope=None, space=None, snapshot=None):
        return []


def _install(monkeypatch, meta):
    fake = FakePraxis(meta)
    monkeypatch.setattr(ts, "_praxis", fake)
    return fake


def test_automated_floor_only_ticket_warns_naming_the_cid(monkeypatch, capsys):
    # verify=automated, an acceptance (so the floor exists), no tags -> resolves ZERO declared checks.
    _install(monkeypatch, {"requirement_id": "R7", "tags": [], "acceptance": "it works",
                           "verify": "automated"})
    reqs = ts.start_ticket("R7", "owner-a", project="team-app")
    # buildable via the floor (the returned contract is non-empty) ...
    assert reqs and any("acceptance" in ts._check_id(r) for r in reqs)
    # ... but the belt loudly surfaces that NO declared gate covers it, naming the cid.
    err = capsys.readouterr().err
    assert "[af-build] WARNING" in err
    assert "R7" in err


def test_manual_floor_only_ticket_does_not_warn(monkeypatch, capsys):
    # A manual ticket's acceptance is a human-confirmed condition; a floor-only manual ticket is
    # expected, not a defect — so no warning.
    _install(monkeypatch, {"requirement_id": "R8", "tags": [], "acceptance": "the flow feels right",
                           "verify": "manual"})
    ts.start_ticket("R8", "owner-a", project="team-app")
    err = capsys.readouterr().err
    assert "[af-build] WARNING" not in err
