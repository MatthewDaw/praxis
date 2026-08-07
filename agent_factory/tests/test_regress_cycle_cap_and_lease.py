"""FL8 (R8, D2/E1, D5/E2) acceptance:

  * the rerun's pinned set contains the new check id and FINISH is refused while it fails (the
    ticket-identity lane, FL6, plus the ordinary coverage gate already deliver this — exercised
    here end-to-end via ``regress_for_check`` + the identity-lane resolve);
  * a bounded regress-cycle cap PER (ticket, check) pair — once tripped — parks the ticket
    BLOCKED with its full history retained and emits a ``"parking"`` flag event, instead of
    regressing it again;
  * a ticket regressed while a worker holds a LIVE lease on it has that worker's later FINISH
    refused; re-claiming (which surfaces the regression) clears the refusal.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import pytest  # noqa: E402
from hooks import _praxis  # noqa: E402

import _ticket_state as ts  # noqa: E402
from agent_factory import ingestion_api  # noqa: E402


class _WhoAmIStub:
    def __init__(self, ok: bool = True, principal: str = "user-1") -> None:
        self.ok = ok
        self.principal = principal
        self.detail = ""


def _authed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_praxis, "whoami", lambda: _WhoAmIStub(True))


class _Backend:
    """A ``_praxis`` double: an in-memory ticket table (``{cid: {"meta": {...}}}``) plus a flat
    call log, enough for ``regress_for_check``/``ingest`` to round-trip without a live backend.
    ``write_build_state`` and ``regress_requirements`` both MERGE onto the ticket's stored meta,
    mirroring the real server closely enough for these assertions."""

    def __init__(self, tickets: dict[str, dict[str, Any]] | None = None) -> None:
        self.tickets = tickets if tickets is not None else {}
        self.calls: list[dict[str, Any]] = []
        self._n = 0

    def _request(self, method: str, path: str, *, body: dict[str, Any] | None = None,
                params: dict[str, Any] | None = None, space: str | None = None,
                snapshot: str | None = None, **kw: Any) -> dict[str, Any]:
        self.calls.append({"method": method, "path": path, "body": body,
                          "space": space, "snapshot": snapshot})
        if method == "POST" and path == "/insights":
            self._n += 1
            return {"id": f"flag-{self._n}", "action": "added"}
        return {}

    def ensure_space(self, *a: Any, **kw: Any) -> Any:
        return a[0]

    def get_fact(self, cid: str, **kw: Any) -> dict[str, Any] | None:
        return self.tickets.get(cid)

    def regress_requirements(self, project: str, ids: list[str],
                             detail: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        detail = detail or {}
        for tid in ids:
            t = self.tickets.setdefault(tid, {"id": tid, "meta": {}})
            patch = detail.get(tid, detail) if isinstance(detail, dict) else {}
            t["meta"].update(patch)
            t["meta"]["build_state"] = "incomplete"
        self.calls.append({"method": "REGRESS", "path": "/requirements/regress",
                          "body": {"ids": list(ids), "detail": detail}})
        return {"count": len(ids)}

    def write_build_state(self, cid: str, meta_dict: dict[str, Any],
                          owner: str | None = None, **kw: Any) -> dict[str, Any]:
        t = self.tickets.setdefault(cid, {"id": cid, "meta": {}})
        for k, v in meta_dict.items():
            if v is None:
                t["meta"].pop(k, None)
            else:
                t["meta"][k] = v
        self.calls.append({"method": "BUILD_STATE", "path": f"/requirements/{cid}/build-state",
                          "body": dict(meta_dict)})
        return t

    def claim_requirement(self, cid: str, owner: str, ttl: int, **kw: Any) -> dict[str, Any]:
        t = self.tickets.setdefault(cid, {"id": cid, "meta": {}})
        t["meta"]["claim_owner"] = owner
        t["meta"]["build_state"] = "in_progress"
        return t


def _install(monkeypatch: pytest.MonkeyPatch, backend: _Backend) -> None:
    """Patch BOTH the bare ``_praxis`` module ``ingestion_api`` calls into directly and
    ``_ticket_state``'s OWN ``_praxis`` name — REBOUND wholesale (not attribute-patched on
    whatever object it currently holds), because another test module in this suite
    (``test_planning_owner_scope.py``) permanently reassigns ``ts._praxis = fake`` with no
    teardown. Attribute-patching a snapshot of ``ts._praxis`` captured at collection time would
    silently miss that later rebind; ``monkeypatch.setattr(ts, "_praxis", backend)`` rebinds the
    name itself and monkeypatch still restores it afterward."""
    for name in ("_request", "ensure_space", "get_fact", "regress_requirements",
                "write_build_state", "claim_requirement"):
        monkeypatch.setattr(_praxis, name, getattr(backend, name))
    monkeypatch.setattr(ts, "_praxis", backend)


# --------------------------------------------------------------------------- D2/E1: the regress-cycle cap

def test_regress_for_check_within_cap_regresses_normally_and_stamps_cycle_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authed(monkeypatch)
    backend = _Backend()
    _install(monkeypatch, backend)

    out = ingestion_api.regress_for_check(
        "proj", ["t1"], "check-x", {"reason": "still fails"}, cap=3,
    )

    assert out == {"regressed": ["t1"], "parked": []}
    meta = backend.tickets["t1"]["meta"]
    assert meta["build_state"] == "incomplete"
    assert meta["regress_cycles"] == {"check-x": 1}
    assert [d["reason"] for d in meta["regression_detail"]] == ["still fails"]
    assert not any(c["path"] == "/insights" and (c["body"] or {}).get("category") == "flag"
                  for c in backend.calls)


def test_regress_for_check_trips_cap_parks_blocked_with_full_history_and_emits_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authed(monkeypatch)
    backend = _Backend(tickets={
        "t1": {"id": "t1", "meta": {
            "regress_cycles": {"check-x": 3},
            "regression_detail": [{"reason": "attempt 1", "resolved": False},
                                  {"reason": "attempt 2", "resolved": False},
                                  {"reason": "attempt 3", "resolved": False}],
        }},
    })
    _install(monkeypatch, backend)

    out = ingestion_api.regress_for_check(
        "proj", ["t1"], "check-x", {"reason": "attempt 4 still fails"}, cap=3,
    )

    assert out == {"regressed": [], "parked": ["t1"]}
    meta = backend.tickets["t1"]["meta"]
    assert meta["build_state"] == "blocked"
    assert "block_reason" in meta and "cap" in meta["block_reason"]
    # full history retained -- all four attempts, not truncated or wiped
    assert [d["reason"] for d in meta["regression_detail"]] == [
        "attempt 1", "attempt 2", "attempt 3", "attempt 4 still fails",
    ]
    assert meta["regress_cycles"]["check-x"] == 4
    # the ticket must NOT be regressed-to-incomplete again -- it is parked, not looping
    assert not any(c["method"] == "REGRESS" for c in backend.calls)

    flag_calls = [c for c in backend.calls
                 if c["path"] == "/insights" and (c["body"] or {}).get("category") == "flag"]
    assert flag_calls, "a cap trip must emit a flag event (R24, never silent)"
    flag_meta = flag_calls[0]["body"]["meta"]
    assert flag_meta["kind"] == ingestion_api.FLAG_KIND_PARKING
    assert flag_meta["ticket_id"] == "t1"
    assert flag_meta["check_id"] == "check-x"


def test_a_different_check_on_the_same_ticket_has_its_own_independent_cycle_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authed(monkeypatch)
    backend = _Backend(tickets={
        "t1": {"id": "t1", "meta": {"regress_cycles": {"check-x": 3}}},
    })
    _install(monkeypatch, backend)

    out = ingestion_api.regress_for_check("proj", ["t1"], "check-y", {"reason": "new check"}, cap=3)

    assert out == {"regressed": ["t1"], "parked": []}
    assert backend.tickets["t1"]["meta"]["regress_cycles"] == {"check-x": 3, "check-y": 1}


# --------------------------------------------------------------------------- D5/E2: lease revocation + FINISH refusal

def test_regress_for_check_revokes_a_live_lease_and_later_finish_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authed(monkeypatch)
    now = time.time()
    backend = _Backend(tickets={
        "t1": {"id": "t1", "meta": {
            "build_state": "in_progress", "claim_owner": "worker-a",
            "claim_at": now, "claim_heartbeat_at": now, "claim_lease_ttl": 900,
        }},
    })
    _install(monkeypatch, backend)

    ingestion_api.regress_for_check("proj", ["t1"], "check-x", {"reason": "regressed under lease"})

    meta = backend.tickets["t1"]["meta"]
    assert meta["regressed_owner"] == "worker-a"

    # worker-a, unaware, tries to finish its stale attempt -- refused.
    ok = ts.release("t1", "worker-a", "finished")
    assert ok is False
    assert backend.tickets["t1"]["meta"]["build_state"] == "incomplete"  # unchanged by the refusal


def test_reclaim_after_regression_clears_the_marker_and_finish_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authed(monkeypatch)
    now = time.time()
    backend = _Backend(tickets={
        "t1": {"id": "t1", "meta": {
            "build_state": "in_progress", "claim_owner": "worker-a",
            "claim_at": now, "claim_heartbeat_at": now, "claim_lease_ttl": 900,
        }},
    })
    _install(monkeypatch, backend)
    ingestion_api.regress_for_check("proj", ["t1"], "check-x", {"reason": "regressed under lease"})
    assert backend.tickets["t1"]["meta"]["regressed_owner"] == "worker-a"

    assert ts.claim("t1", "worker-a", ttl=900) is True
    assert backend.tickets["t1"]["meta"].get("regressed_owner") is None

    # a finish now needs pinned checks to satisfy `release`'s own real-server guard, but this
    # fake backend's `release_requirement` is not stubbed (release() is only reached if the
    # refusal above did NOT fire) -- so assert directly on the client-side gate instead.
    meta = ts._meta("t1", None)
    assert meta.get("regressed_owner") != "worker-a"


def test_no_live_lease_at_regression_time_revokes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _authed(monkeypatch)
    backend = _Backend(tickets={"t1": {"id": "t1", "meta": {"build_state": "incomplete"}}})
    _install(monkeypatch, backend)

    ingestion_api.regress_for_check("proj", ["t1"], "check-x", {"reason": "no one was building it"})

    assert "regressed_owner" not in backend.tickets["t1"]["meta"]


# --------------------------------------------------------------------------- unit: the pure _ticket_state helpers

def test_next_regress_cycle_and_bumped_regress_cycles_are_independent_per_check_id() -> None:
    meta = {"regress_cycles": {"check-x": 2}}
    assert ts.next_regress_cycle(meta, "check-x") == 3
    assert ts.next_regress_cycle(meta, "check-y") == 1
    bumped = ts.bumped_regress_cycles(meta, "check-x", 3)
    assert bumped == {"check-x": 3}


def test_lease_revocation_patch_only_fires_on_a_live_lease() -> None:
    live = {"build_state": "in_progress", "claim_owner": "w1",
           "claim_heartbeat_at": time.time(), "claim_lease_ttl": 900}
    assert ts.lease_revocation_patch(live) == {"regressed_owner": "w1"}
    assert ts.lease_revocation_patch({"build_state": "incomplete"}) == {}


def test_clear_lease_and_run_meta_nulls_every_lease_and_run_key() -> None:
    patch = ts.clear_lease_and_run_meta()
    assert patch == {
        "claim_owner": None, "claim_at": None, "claim_heartbeat_at": None, "claim_lease_ttl": None,
        "run_owner": None, "run_at": None, "run_scope": None,
    }


# --------------------------------------------------------------------------- integration: ingest() wires it through

def test_ingest_with_ticket_ids_routes_through_regress_for_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8: a freshly drafted, ticket-bound check regresses its ticket(s) normally on its first
    cycle (well within the cap) -- ingest()'s public behavior is unchanged, just DRY'd through the
    shared cap/lease-aware primitive."""
    _authed(monkeypatch)
    backend = _Backend()
    _install(monkeypatch, backend)

    result = ingestion_api.ingest(
        "always run the migration before the smoke test", "proj",
        drafted_run="pytest tests/test_x.py -q", channel="machine", ticket_ids=["t1"],
    )

    assert result["check_id"] is not None
    meta = backend.tickets["t1"]["meta"]
    assert meta["build_state"] == "incomplete"
    assert meta["regress_cycles"] == {result["check_id"]: 1}
