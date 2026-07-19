"""Deliverable 5: a verify="manual" ticket cannot self-certify from a worker-run pass.

The acceptance floor inherits ``meta.verify``, so a manual ticket's floor lands in
``meta.manual_requirements``. ``all_validations_passed`` then holds that requirement to the
stricter bar: a covering validation must have passed with a human/external ``source`` — a
worker-run pass (the default) never counts. An automated ticket has an empty manual set, so a
worker-run pass alone is enough; this proves the gate ONLY tightens manual tickets.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402

PLAN = ("team-app", "prd-team-app")


class FakePraxis:
    """Persists ONE ticket's meta across calls; ``patch_meta`` MERGES like the real server."""

    def __init__(self, meta):
        self._meta = dict(meta)

    def get_fact(self, cid, *, space=None, snapshot=None):
        return {"id": cid, "meta": dict(self._meta)}

    def patch_meta(self, cid, meta_dict, *, space=None, snapshot=None):
        self._meta.update(meta_dict)  # MERGE, matching the server's PATCH semantics
        return {"id": cid, "meta": dict(self._meta)}


def _install(monkeypatch, meta):
    fake = FakePraxis(meta)
    monkeypatch.setattr(ts, "_praxis", fake)
    # Only the acceptance floor should resolve: no checks, no surfaces.
    monkeypatch.setattr(ts._praxis, "facts_by", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(ts._praxis, "surface_checks", lambda *a, **k: [], raising=False)
    return fake


def test_manual_ticket_not_passed_by_worker_self_cert(monkeypatch):
    fake = _install(monkeypatch, {
        "requirement_id": "R1", "tags": [],
        "acceptance": "UX feels instant", "verify": "manual",
    })

    # 1) Start the ticket — the acceptance floor inherits verify=manual.
    ts.start_ticket("R1", "owner", project="team-app")
    assert "R1::acceptance" in fake._meta[ts.M_MANUAL_REQUIREMENTS]
    assert "R1::acceptance" in fake._meta[ts.M_REQUIRED_VALIDATIONS]

    # 2) Worker authors + pins a covering validation and records a worker-run pass.
    ts.pin_validations("R1", [{"validation_id": "v1", "covers": ["R1::acceptance"],
                               "run": "echo ok"}], ref=PLAN)
    ts.record_validation_pass("R1", "v1", True, ref=PLAN)  # default source="worker"

    # The manual requirement was self-certified by the worker — that does NOT count.
    assert ts.all_validations_passed("R1", ref=PLAN) is False

    # 3) Re-record the SAME validation with a human source — now it counts.
    ts.record_validation_pass("R1", "v1", True, source="human", ref=PLAN)
    assert ts.all_validations_passed("R1", ref=PLAN) is True


def test_automated_ticket_passes_on_worker_pass_alone(monkeypatch):
    # No verify key -> acceptance floor defaults to "automated"; manual set stays empty.
    fake = _install(monkeypatch, {
        "requirement_id": "R2", "tags": [], "acceptance": "returns 200",
    })

    ts.start_ticket("R2", "owner", project="team-app")
    assert fake._meta[ts.M_MANUAL_REQUIREMENTS] == []
    assert "R2::acceptance" in fake._meta[ts.M_REQUIRED_VALIDATIONS]

    ts.pin_validations("R2", [{"validation_id": "v1", "covers": ["R2::acceptance"],
                               "run": "curl -s -o /dev/null -w '%{http_code}'"}], ref=PLAN)
    ts.record_validation_pass("R2", "v1", True, ref=PLAN)  # worker source

    # No manual requirement to gate on — a worker-run pass alone finishes an automated ticket.
    assert ts.all_validations_passed("R2", ref=PLAN) is True
