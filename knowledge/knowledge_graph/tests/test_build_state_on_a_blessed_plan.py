"""Build state must be writable on a BLESSED plan. Plan content must not be.

THE BUG THIS LOCKS. Every ticket state write in the build loop went through
``PATCH /candidates/{cid}`` (``FactsCandidates.update``), which the S12 bless guard
refuses once a ``prd-<project>`` snapshot is blessed. So on a blessed plan -- the only
kind a build ever runs against -- a worker could not claim a ticket, could not pin the
checks it must pass, and could not finish. Observed live: a loop dispatched three
tickets, all three workers failed to claim, zero worktrees and zero branches were
created, and the run burned half an hour spinning. The refused check-pin was worse than
the refused claim, because it was SILENT: the ticket kept ``pinned_checks: []``, which
reads afterwards as "RESOLVE never ran", and an audit found 9 finished tickets in that
state.

The guard is RIGHT. Build state simply is not plan content -- it is what the loop learns
while EXECUTING the plan -- so it now travels on its own routes, which the guard does not
sit on. These tests pin both halves against the same blessed snapshot, because either one
alone proves nothing: that the old route is still refused (the guard was not weakened),
and that the new ones now succeed (the lockout is actually gone).
"""

from __future__ import annotations

import json

import pytest

from knowledge.serve.facts_candidates import FactsCandidates
from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
    LeaseConflict,
    PostgresVectorGraph,
)

SPACE = "team-app"
BLESSED_PLAN = "prd-team-app"


class _FakeCursor:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _FakeConn:
    """Replays a canned meta row for reads and records every statement issued."""

    def __init__(self, meta):
        self.meta = dict(meta)
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.statements.append((sql, tuple(params or ())))
        if sql.lstrip().upper().startswith("SELECT"):
            if "meta->>'claim_owner'" in sql and "SELECT meta FROM" not in sql:
                return _FakeCursor((self.meta.get("claim_owner"),))
            return _FakeCursor((json.dumps(self.meta),))
        return _FakeCursor((json.dumps(self.meta),))  # the UPDATE's RETURNING meta

    @property
    def updates(self) -> list[tuple[str, tuple]]:
        return [s for s in self.statements if s[0].lstrip().upper().startswith("UPDATE")]


def _graph(meta) -> PostgresVectorGraph:
    """A snapshot-bound graph with just enough wired for the build-state paths."""
    g = PostgresVectorGraph.__new__(PostgresVectorGraph)
    g._conn = _FakeConn(meta)
    g._facts_table = "snapshots"
    g._space = SPACE
    g._snapshot = BLESSED_PLAN
    g._key_pred = lambda: ("TRUE", ())
    g._claim_view = lambda m, epoch: m
    g._server_epoch = lambda: 0
    return g


class _BlessedMarker:
    """A planning marker with NO owner -- exactly what ``clear_planning`` leaves at bless."""

    id = "prd-team-app::planning"
    meta = {"blessed_at": 1.0}


class _StubGraph:
    def find_planning_marker(self, project):
        return _BlessedMarker()

    def get_fact(self, cid):  # pragma: no cover - the guard must raise before this
        raise AssertionError("the bless guard must refuse before reading the fact")


def _candidates_on_a_blessed_plan() -> FactsCandidates:
    svc = FactsCandidates.__new__(FactsCandidates)
    svc.graph = _StubGraph()
    svc._facts_table = "snapshots"
    svc._space = SPACE
    svc._snapshot = BLESSED_PLAN
    return svc


# ------------------------------------------------- the guard is intact: patch_meta is refused

def test_the_patch_meta_route_is_still_refused_on_a_blessed_plan():
    """The half that must NOT change. If this ever passes, the fix punched a hole."""
    svc = _candidates_on_a_blessed_plan()
    with pytest.raises(ValueError) as exc:
        svc.update("ticket-1", {"meta": {"pinned_checks": [{"validation_id": "v1"}]}})
    assert "blessed" in str(exc.value)
    assert "planning marker" in str(exc.value)


# ------------------------------------------- the lockout is gone: the sanctioned routes write

def test_pinning_checks_succeeds_on_a_blessed_plan():
    g = _graph({"requirement_id": "R1", "pinned_checks": []})
    pinned = [{"validation_id": "v1", "covers": ["c1"], "run": "pytest", "passed": None}]

    g.write_build_state("ticket-1", {"pinned_checks": pinned})

    assert g._conn.updates, "the pin must actually write on a blessed plan"
    sql, params = g._conn.updates[-1]
    assert json.loads(params[0])["pinned_checks"] == pinned


def test_claiming_succeeds_on_a_blessed_plan():
    g = _graph({"requirement_id": "R1"})
    g.claim_requirement("ticket-1", "worker-a", 900)
    sql, params = g._conn.updates[-1]
    assert "'build_state', 'in_progress'" in sql
    assert "worker-a" in params


def test_finishing_succeeds_on_a_blessed_plan_once_checks_are_pinned():
    g = _graph({
        "requirement_id": "R1",
        "claim_owner": "worker-a",
        "pinned_checks": [{"validation_id": "v1", "passed": True}],
    })
    g.release_requirement("ticket-1", "worker-a", "finished")
    sql, params = g._conn.updates[-1]
    assert "'build_state', %s::text" in sql and "finished" in params


# -------------------------------------------------------------- ordering: pin BEFORE finish

def test_finishing_before_the_pin_lands_is_refused_and_writes_nothing():
    """The migration's ordering implication, stated as a test.

    The finish guard reads ``pinned_checks``, so RESOLVE's pin must LAND first. When the
    pin was silently refused (the bug above), this is the failure the loop now hits
    loudly at finish instead of certifying an ungated ticket."""
    g = _graph({"requirement_id": "R1", "claim_owner": "worker-a", "pinned_checks": []})
    with pytest.raises(ValueError) as exc:
        g.release_requirement("ticket-1", "worker-a", "finished")
    assert "pinned_checks is empty" in str(exc.value)
    assert not g._conn.updates


# ------------------------------------------------------- the new route is not a second hole

@pytest.mark.parametrize(
    "plan_content",
    [
        {"text": "a different requirement entirely"},
        {"tags": ["auth"]},
        {"acceptance": "something easier"},
        {"depends_on": []},
        {"requirement_id": "R99"},
    ],
)
def test_plan_content_is_refused_by_the_build_state_route(plan_content):
    """The route carries build state AROUND the guard; it must not carry plan content."""
    g = _graph({"requirement_id": "R1"})
    with pytest.raises(ValueError) as exc:
        g.write_build_state("ticket-1", plan_content)
    assert "is not build state" in str(exc.value)
    assert not g._conn.updates, "a rejected patch must write nothing at all"


@pytest.mark.parametrize("state", ["finished", "in_progress", "incomplete"])
def test_the_build_state_route_cannot_make_the_guarded_transitions(state):
    """Stamping ``finished`` here would route straight around the finish guard."""
    g = _graph({"requirement_id": "R1"})
    with pytest.raises(ValueError) as exc:
        g.write_build_state("ticket-1", {"build_state": state})
    assert "may not be set here" in str(exc.value)
    assert not g._conn.updates


def test_blocked_is_the_one_transition_the_route_makes():
    g = _graph({"requirement_id": "R1"})
    g.write_build_state("ticket-1", {"build_state": "blocked", "block_reason": "needs a secret"})
    _, params = g._conn.updates[-1]
    assert json.loads(params[0])["build_state"] == "blocked"


def test_a_caller_cannot_smuggle_in_a_finished_at():
    """The server owns that clock; a second producer is exactly the drift it removed."""
    g = _graph({"requirement_id": "R1"})
    g.write_build_state("ticket-1", {"run_at": 1.0, "finished_at": "2020-01-01T00:00:00.000000+00:00"})
    _, params = g._conn.updates[-1]
    assert "finished_at" not in json.loads(params[0])


# --------------------------------------------------------------------- write semantics

def test_a_none_value_removes_the_key():
    """``block``/``clear_run`` mean ABSENCE, which a jsonb merge cannot express."""
    g = _graph({"requirement_id": "R1", "claim_owner": "worker-a"})
    g.write_build_state("ticket-1", {"claim_owner": None, "claim_lease_ttl": None,
                                     "block_reason": "stuck"})
    sql, params = g._conn.updates[-1]
    assert "- 'claim_owner'" in sql and "- 'claim_lease_ttl'" in sql
    assert json.loads(params[0]) == {"block_reason": "stuck"}


def test_the_lease_is_enforced_when_an_owner_is_supplied():
    g = _graph({"requirement_id": "R1", "claim_owner": "worker-b"})
    g._lease_conflict = lambda fid: (_ for _ in ()).throw(LeaseConflict("worker-b", 42.0))
    with pytest.raises(LeaseConflict):
        g.write_build_state("ticket-1", {"pinned_checks": []}, owner="worker-a")
    assert not g._conn.updates


def test_the_lease_is_not_required_for_the_run_marker():
    """The run marker is stamped across the in-scope set BEFORE anything is claimed."""
    g = _graph({"requirement_id": "R1"})
    g.write_build_state("ticket-1", {"run_owner": "run-1", "run_at": 2.0, "run_scope": "all"})
    assert g._conn.updates


def test_finishing_clears_the_run_marker_but_a_yield_keeps_it():
    """A finished ticket has LEFT the run; a yield stays in scope so the run re-does it."""
    meta = {"requirement_id": "R1", "claim_owner": "w",
            "pinned_checks": [{"validation_id": "v1"}], "run_owner": "run-1"}
    finished = _graph(dict(meta))
    finished.release_requirement("ticket-1", "w", "finished")
    assert "- 'run_owner'" in finished._conn.updates[-1][0]

    yielded = _graph(dict(meta))
    yielded.release_requirement("ticket-1", "w", "incomplete")
    assert "- 'run_owner'" not in yielded._conn.updates[-1][0]


def test_a_finish_survives_a_lease_takeover_only_when_asked():
    """Completion is a fact about the world; a YIELD is a claim about the current attempt."""
    meta = {"requirement_id": "R1", "claim_owner": "worker-b",
            "pinned_checks": [{"validation_id": "v1"}]}

    honored = _graph(dict(meta))
    honored.release_requirement("ticket-1", "worker-a", "finished", honor_takeover=True)
    sql, params = honored._conn.updates[-1]
    assert "claim_owner' = %s" not in sql, "the owner predicate is dropped for an honored finish"
    assert "worker-a" not in params

    guarded = _graph(dict(meta))
    guarded.release_requirement("ticket-1", "worker-a", "incomplete")
    sql, params = guarded._conn.updates[-1]
    assert "claim_owner' = %s" in sql and "worker-a" in params


def test_an_unclaimed_ticket_can_be_released_by_anyone():
    """No lease held == no active build to protect; refusing would strand swept work."""
    g = _graph({"requirement_id": "R1", "pinned_checks": [{"validation_id": "v1"}]})
    g.release_requirement("ticket-1", "worker-a", "incomplete")
    sql, _ = g._conn.updates[-1]
    assert "claim_owner' IS NULL" in sql
