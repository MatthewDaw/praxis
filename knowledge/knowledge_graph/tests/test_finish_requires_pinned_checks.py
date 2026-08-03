"""FINISH must not certify a ticket that nothing gates.

``release_requirement(state="finished")`` is the single chokepoint every finish path
funnels through. If it accepts a ticket whose ``pinned_checks`` is empty, the ticket
certifies itself: ``build_state="finished"`` -- the strongest claim the system can make
-- ends up resting on no evidence. An audit of one 260-ticket plan found exactly that,
9 finished tickets with zero pinned checks (a whole auth-migration lane) whose RESOLVE
step had silently never run. Nothing errored; the absence of a signal read as success.

These tests pin the guard from both sides. A guard only ever seen to pass is worth
little, so the refusing case is asserted first and explicitly.
"""

from __future__ import annotations

import json

import pytest

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
    PostgresVectorGraph,
)


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    """Records statements and replays a canned meta row for the guard's SELECT."""

    def __init__(self, meta):
        self._meta = meta
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if sql.lstrip().upper().startswith("SELECT"):
            return _FakeCursor((json.dumps(self._meta),))
        # The UPDATE path: echo back a plausible post-write meta.
        written = dict(self._meta)
        written["build_state"] = (params or [None])[0]
        return _FakeCursor((json.dumps(written),))


def _graph(meta):
    """A graph instance with just enough wired for release_requirement."""
    g = PostgresVectorGraph.__new__(PostgresVectorGraph)
    g._conn = _FakeConn(meta)
    g._facts_table = "snapshots"
    g._key_pred = lambda: ("TRUE", ())
    g._lease_conflict = lambda fid: pytest.fail(f"unexpected lease conflict for {fid}")
    g._claim_view = lambda m, epoch: m
    g._server_epoch = lambda: 0
    return g


def test_finish_is_refused_when_pinned_checks_is_empty():
    g = _graph({"requirement_id": "R1", "claim_owner": "w", "pinned_checks": []})
    with pytest.raises(ValueError) as exc:
        g.release_requirement("fact-1", "w", "finished")
    msg = str(exc.value)
    assert "pinned_checks is empty" in msg
    assert "certify itself" in msg
    # It must refuse BEFORE writing anything -- a rejected finish that already
    # mutated build_state would be worse than no guard at all.
    assert not any(s.lstrip().upper().startswith("UPDATE") for s in g._conn.statements)


def test_finish_is_refused_when_pinned_checks_key_is_absent():
    # An absent key and an empty list are the same claim: nothing gates this ticket.
    g = _graph({"requirement_id": "R2", "claim_owner": "w"})
    with pytest.raises(ValueError):
        g.release_requirement("fact-2", "w", "finished")


def test_finish_is_allowed_with_pinned_checks():
    g = _graph(
        {
            "requirement_id": "R3",
            "claim_owner": "w",
            "pinned_checks": [{"validation_id": "x", "passed": True}],
        }
    )
    g.release_requirement("fact-3", "w", "finished")
    assert any(s.lstrip().upper().startswith("UPDATE") for s in g._conn.statements)


def test_finish_is_allowed_with_an_explicit_recorded_waiver():
    # The escape hatch exists so a legitimate exception reads AS one in the data,
    # rather than being indistinguishable from checks that never resolved.
    g = _graph(
        {
            "requirement_id": "R4",
            "claim_owner": "w",
            "pinned_checks": [],
            "checks_waived_reason": "pure documentation ticket; no executable surface",
        }
    )
    g.release_requirement("fact-4", "w", "finished")
    assert any(s.lstrip().upper().startswith("UPDATE") for s in g._conn.statements)


def test_a_blank_waiver_string_does_not_count_as_a_waiver():
    g = _graph({"requirement_id": "R5", "claim_owner": "w", "checks_waived_reason": "   "})
    with pytest.raises(ValueError):
        g.release_requirement("fact-5", "w", "finished")


def test_yielding_incomplete_is_never_blocked():
    # Yielding a ticket with no checks is honest -- only claiming DONE needs a contract.
    g = _graph({"requirement_id": "R6", "claim_owner": "w", "pinned_checks": []})
    g.release_requirement("fact-6", "w", "incomplete")
    assert any(s.lstrip().upper().startswith("UPDATE") for s in g._conn.statements)
    assert not any(s.lstrip().upper().startswith("SELECT") for s in g._conn.statements)
