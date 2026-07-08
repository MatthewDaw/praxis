"""Locks #4: factory ticket STATE binds to the plan snapshot.

Every state read/write threads the plan ``ref = project_ref(project).plan`` down to
``_praxis`` as the ``(space, snapshot)`` tenancy headers, so build_state/claims/pins/
outcomes land on ``(space=<project>, snapshot=prd-<project>)`` — NOT working memory.
Passing no ref keeps the working-memory default (the back-compat lane).
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402


class FakePraxis:
    """Records every state call's (space, snapshot) so the test can assert binding."""

    def __init__(self, meta=None):
        self._meta = meta or {}
        self.writes = []   # (cid, meta_patch, space, snapshot)
        self.reads = []    # (cid, space, snapshot)
        self.outcomes = []  # (cid, success, space, snapshot)

    def get_fact(self, cid, *, space=None, snapshot=None):
        self.reads.append((cid, space, snapshot))
        return {"id": cid, "meta": dict(self._meta)}

    def patch_meta(self, cid, meta_dict, *, space=None, snapshot=None):
        self.writes.append((cid, meta_dict, space, snapshot))
        self._meta.update(meta_dict)
        return {"id": cid, "meta": dict(self._meta)}

    def record_outcome(self, cid, success, *, space=None, snapshot=None):
        self.outcomes.append((cid, success, space, snapshot))


def _install(monkeypatch, meta=None):
    fake = FakePraxis(meta)
    monkeypatch.setattr(ts, "_praxis", fake)
    return fake


def test_claim_binds_state_to_plan_snapshot(monkeypatch):
    fake = _install(monkeypatch)
    assert ts.claim("R1", "owner-a", ref=("team-app", "prd-team-app")) is True
    # both the lease read and the write carry the plan (space, snapshot).
    assert fake.reads == [("R1", "team-app", "prd-team-app")]
    cid, patch, space, snap = fake.writes[-1]
    assert (cid, space, snap) == ("R1", "team-app", "prd-team-app")
    assert patch["build_state"] == "in_progress"


def test_no_ref_defaults_to_working_memory(monkeypatch):
    fake = _install(monkeypatch)
    assert ts.claim("R1", "owner-a") is True
    assert fake.reads == [("R1", None, None)]           # working memory (no space header)
    assert fake.writes[-1][2:] == (None, None)


def test_pin_and_record_pass_thread_the_ref(monkeypatch):
    fake = _install(monkeypatch)
    ref = ("team-app", "prd-team-app")
    ts.pin_requirements("R1", [{"id": "c1"}], ref=ref)
    ts.record_validation_pass("R1", "c1", True, ref=ref)
    assert all((s, n) == ref for _, _, s, n in fake.writes)


def test_start_ticket_derives_and_threads_the_plan_ref(monkeypatch):
    # A ticket with a tag so resolve returns something; no checks -> the acceptance floor covers it.
    fake = _install(monkeypatch, meta={"requirement_id": "R1", "tags": [], "acceptance": "it works"})
    # facts_by/surface_checks are only hit for check reads; stub them empty.
    monkeypatch.setattr(ts._praxis, "facts_by", lambda *a, **k: [], raising=False)
    ts.start_ticket("R1", "owner-a", project="team-app")
    # every STATE write (claim + pin) bound to the plan snapshot, derived from project.
    assert fake.writes, "expected state writes"
    assert all((s, n) == ("team-app", "prd-team-app") for _, _, s, n in fake.writes)
