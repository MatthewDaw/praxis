"""Locks the NEW build-side behavior in ``hooks/_ticket_state.py``:

  * dependency-aware popping — ``next_ready_ticket`` / ``ready_tickets`` / ``is_ready`` /
    ``pending_deps`` / ``unfinished_ids`` (only claim a ticket whose prerequisites are finished),
  * two-tier validation — ``coverage_gap`` / ``all_validations_passed`` (a ticket is done iff every
    resolved requirement is covered by a pinned validation AND every validation passed),
  * whole-set run marker staleness — ``run_live``,
  * validation normalization — ``_norm_validation``.

These pure helpers take an already-fetched fact dict (``{"id":..., "meta": {...}}``), so they never
touch Praxis — no network, fully deterministic.
"""

import sys
import time
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402


def fact(rid, **meta):
    m = {"requirement_id": rid}
    m.update(meta)
    return {"id": rid, "text": rid, "meta": m}


# --------------------------------------------------------------------------- dependency readiness

def test_ready_excludes_tickets_waiting_on_unfinished_prereq():
    items = [fact("R1", depends_on=["R2"]), fact("R2"), fact("R3", build_state="finished")]
    # R2 is free; R1 waits on R2 (unfinished); R3 is finished (not claimable).
    assert [t["id"] for t in ts.ready_tickets(items)] == ["R2"]
    assert ts.next_ready_ticket(items)["id"] == "R2"


def test_dependency_satisfied_when_prereq_absent_from_incomplete_set():
    # A finished prereq is dropped by the server's incomplete view, so an absent dep == satisfied.
    items = [fact("R2", depends_on=["R1"])]
    assert ts.next_ready_ticket(items)["id"] == "R2"
    assert ts.pending_deps(items[0], ts.unfinished_ids(items)) == []


def test_in_progress_prereq_blocks_dependent():
    items = [fact("R1", build_state="in_progress"), fact("R2", depends_on=["R1"])]
    # R1 in_progress counts as UNFINISHED, so its dependent R2 is not ready. (R1 itself is still
    # dependency-ready — lease ownership is enforced separately at claim/exclude_leased, not here.)
    unfinished = ts.unfinished_ids(items)
    assert ts.pending_deps(items[1], unfinished) == ["R1"]
    assert ts.is_ready(items[1], unfinished) is False
    assert [t["id"] for t in ts.ready_tickets(items)] == ["R1"]


def test_cycle_yields_no_ready_ticket():
    items = [fact("A", depends_on=["B"]), fact("B", depends_on=["A"])]
    assert ts.ready_tickets(items) == []
    assert ts.next_ready_ticket(items) is None


def test_blocked_ticket_is_never_ready():
    items = [fact("R1", build_state="blocked", block_reason="needs creds")]
    assert ts.ready_tickets(items) == []


def test_depends_on_matches_requirement_id_or_fact_id():
    # depends_on may name the plan requirement id even when the fact id differs.
    dep = {"id": "fact-xyz", "meta": {"requirement_id": "R1"}}
    waiter = fact("R2", depends_on=["R1"])
    unfinished = ts.unfinished_ids([dep, waiter])
    assert ts.is_ready(waiter, unfinished) is False  # R1 still unfinished -> blocked
    unfinished_after = ts.unfinished_ids([waiter])    # R1 gone (finished) -> satisfied
    assert ts.is_ready(waiter, unfinished_after) is True


# --------------------------------------------------------------------------- two-tier coverage

def test_coverage_gap_lists_uncovered_requirements():
    f = fact("R1", required_validations=["r1", "r2"],
             pinned_checks=[{"validation_id": "v1", "covers": ["r1"], "passed": True}])
    assert ts.coverage_gap(f) == ["r2"]


def test_done_requires_full_coverage_and_all_passing():
    # gap present -> not done, even though the one pinned validation passed.
    gapped = fact("R1", required_validations=["r1", "r2"],
                  pinned_checks=[{"validation_id": "v1", "covers": ["r1"], "passed": True}])
    assert ts.all_validations_passed(gapped) is False

    # full coverage but one validation unrun -> not done.
    unrun = fact("R1", required_validations=["r1", "r2"],
                 pinned_checks=[{"validation_id": "v1", "covers": ["r1", "r2"], "passed": None}])
    assert ts.all_validations_passed(unrun) is False

    # full coverage + all pass -> done.
    done = fact("R1", required_validations=["r1", "r2"],
                pinned_checks=[{"validation_id": "v1", "covers": ["r1", "r2"], "passed": True}])
    assert ts.coverage_gap(done) == []
    assert ts.all_validations_passed(done) is True


def test_no_requirements_is_not_silently_done():
    # A ticket that resolved zero requirements cannot self-certify "done" -> must be False (block path).
    assert ts.all_validations_passed(fact("R1", required_validations=[], pinned_checks=[])) is False
    assert ts.all_validations_passed(
        fact("R1", pinned_checks=[{"validation_id": "v", "covers": [], "passed": True}])
    ) is False


# --------------------------------------------------------------------------- acceptance floor

def test_acceptance_floor_added_when_no_checks_resolve():
    # The deadlock fix: zero resolved Praxis checks must still yield a non-empty contract (the
    # ticket's own acceptance condition), so there is always exactly one thing to validate.
    reqs = ts.contract_with_floor("R1", "given X, system does Y observable via Z", resolved=[])
    assert [r["id"] for r in reqs] == ["R1::acceptance"]
    assert reqs[0]["text"] == "given X, system does Y observable via Z"


def test_acceptance_floor_prepended_to_resolved_checks():
    resolved = [{"id": "CHK-7"}, {"id": "CHK-9"}]
    reqs = ts.contract_with_floor("R1", "acc", resolved)
    assert [r["id"] for r in reqs] == ["R1::acceptance", "CHK-7", "CHK-9"]


def test_acceptance_floor_not_duplicated():
    resolved = [{"id": "R1::acceptance"}]
    reqs = ts.contract_with_floor("R1", "acc", resolved)
    assert [r["id"] for r in reqs] == ["R1::acceptance"]


def test_no_acceptance_and_no_checks_is_empty_contract_block_path():
    # Neither a Praxis check nor an acceptance condition -> empty contract -> the worker must block()
    # (a planning defect), never wedge. The floor cannot invent something to prove.
    assert ts.contract_with_floor("R1", "", resolved=[]) == []
    assert ts.contract_with_floor("R1", None, resolved=[]) == []


def test_floored_contract_closes_when_acceptance_validation_passes():
    # End-to-end shape: pin the floor as the contract, cover it with the acceptance test, pass it -> done.
    floored = fact("R1", required_validations=["R1::acceptance"],
                   pinned_checks=[{"validation_id": "accept-test", "covers": ["R1::acceptance"],
                                   "run": "pytest -q test_acc.py", "passed": True}])
    assert ts.coverage_gap(floored) == []
    assert ts.all_validations_passed(floored) is True


# --------------------------------------------------------------------------- run marker staleness

def test_run_live_fresh_stale_and_absent():
    now = time.time()
    assert ts.run_live({"run_owner": "s", "run_at": now}) is True
    assert ts.run_live({"run_owner": "s", "run_at": now - ts.DEFAULT_RUN_TTL_S - 1}) is False
    assert ts.run_live({}) is False
    assert ts.run_live({"run_owner": "s"}) is False  # no run_at


# --------------------------------------------------------------------------- validation normalization

def test_norm_validation_shape_and_synthesized_id():
    v = ts._norm_validation({"covers": "r1", "run": "pytest -q"}, 0)
    assert v == {"validation_id": "r1#0", "covers": ["r1"], "run": "pytest -q",
                 "passed": None, "ran_at": None}
