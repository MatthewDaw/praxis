"""Locks item 7: infra BLOCK is surfaced, never a silent forever-deadlock (``hooks/_ticket_state.py``).

``block(cid, owner, reason)`` marks a ticket TERMINALLY blocked: build_state="blocked" + block_reason,
with BOTH the lease and the whole-set run marker NULLED (the ticket has left the run; only owner action
clears it). The dependency-readiness helpers then treat a blocked ticket as (a) EXCLUDED from the
claimable frontier (``ready_tickets`` / ``next_ready_ticket`` skip it) yet (b) NOT finished
(``unfinished_ids`` still lists it) — so the run completes AROUND it (its dependents stay unready) while
the ticket stays visibly ``blocked``, never wedged and never silently "done".
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402

PLAN = ("team-app", "prd-team-app")


class FakePraxis:
    """Persists one ticket's meta across get_fact/patch_meta (MERGE), like the live server's PATCH."""

    def __init__(self, meta=None):
        self._meta = dict(meta or {})

    def get_fact(self, cid, *, space=None, snapshot=None):
        return {"id": cid, "meta": dict(self._meta)}

    def patch_meta(self, cid, meta_dict, *, space=None, snapshot=None):
        self._meta.update(meta_dict)
        return {"id": cid, "meta": dict(self._meta)}


def test_block_sets_blocked_and_nulls_lease_and_run_marker(monkeypatch):
    # Seed a LIVE claim + run marker so we can prove block() nulls them.
    fake = FakePraxis({
        ts.M_BUILD_STATE: "in_progress",
        ts.M_CLAIM_OWNER: "owner", ts.M_CLAIM_AT: 123.0,
        ts.M_CLAIM_HEARTBEAT_AT: 123.0, ts.M_CLAIM_LEASE_TTL: 900,
        ts.M_RUN_OWNER: "owner", ts.M_RUN_AT: 123.0, ts.M_RUN_SCOPE: "all",
    })
    monkeypatch.setattr(ts, "_praxis", fake)

    assert ts.block("R1", "owner", "needs live Cognito pool", ref=PLAN) is True
    meta = fake.get_fact("R1")["meta"]

    assert meta[ts.M_BUILD_STATE] == "blocked"
    assert meta[ts.M_BLOCK_REASON] == "needs live Cognito pool"
    # lease keys NULLED
    for k in (ts.M_CLAIM_OWNER, ts.M_CLAIM_AT, ts.M_CLAIM_HEARTBEAT_AT, ts.M_CLAIM_LEASE_TTL):
        assert meta[k] is None
    # run-marker keys NULLED (the ticket left the active run)
    for k in (ts.M_RUN_OWNER, ts.M_RUN_AT, ts.M_RUN_SCOPE):
        assert meta[k] is None


def test_blocked_ticket_is_surfaced_but_excluded_from_the_frontier(monkeypatch):
    fake = FakePraxis({
        ts.M_BUILD_STATE: "in_progress",
        ts.M_CLAIM_OWNER: "owner", ts.M_CLAIM_AT: 1.0,
        ts.M_CLAIM_HEARTBEAT_AT: 1.0, ts.M_CLAIM_LEASE_TTL: 900,
        ts.M_RUN_OWNER: "owner", ts.M_RUN_AT: 1.0, ts.M_RUN_SCOPE: "all",
    })
    monkeypatch.setattr(ts, "_praxis", fake)
    assert ts.block("R1", "owner", "needs live Cognito pool", ref=PLAN) is True

    blocked = fake.get_fact("R1")                         # build_state == "blocked"
    dependent = {"id": "R2", "meta": {ts.M_BUILD_STATE: "incomplete", ts.M_DEPENDS_ON: ["R1"]}}
    independent = {"id": "R3", "meta": {ts.M_BUILD_STATE: "incomplete"}}
    items = [blocked, dependent, independent]

    # NOT finished — the blocked ticket is still counted unfinished (surfaced, not silently done).
    assert "R1" in ts.unfinished_ids(items)

    # EXCLUDED from the claimable frontier ...
    ready_ids = {it["id"] for it in ts.ready_tickets(items)}
    assert "R1" not in ready_ids
    # ... its dependent stays unready (R1 never finishes) ...
    assert "R2" not in ready_ids
    # ... but the independent ticket is still worked — the run completes AROUND the block, never wedged.
    assert ready_ids == {"R3"}
    assert ts.next_ready_ticket(items)["id"] == "R3"

    # And the block is visibly surfaced, not lost.
    assert blocked["meta"][ts.M_BUILD_STATE] == "blocked"
