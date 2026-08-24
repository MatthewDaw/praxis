"""A ticket with NO build_state still owes work — and the driver has to see it.

Nothing stamps ``build_state`` when a plan is blessed: the first writer is the worker's claim. So
every ticket in a freshly-blessed plan has no state at all.

Two of the driver's embedded blocks asked for membership in an INCLUSION list,
``build_state in ('incomplete', 'in_progress')``, which does not contain "no state":

  * ``claimable`` -- a blessed 21-ticket plan read ``claimable=0``. The loop logged
    "drained -- nothing claimable" and exited clean, having built nothing, and the run looked
    from the outside exactly like a successful one.
  * ``batch_open`` -- worse, because it decides when a round is over. A ticket dispatched THIS
    round has no build_state until its worker claims it, so the round's own freshly-sent work
    counted as already CLOSED and the round could declare itself complete before a single worker
    had started.

``ready_tickets``/``unfinished_ids`` had always written the same idea as an EXCLUSION of the
terminal states, so two spellings of one predicate disagreed about the same ticket. There is one
predicate now (``ts.is_open_state``), and these tests execute the driver's SHIPPED bytes against it
-- a unit test of the predicate alone would stay green through a revert of the driver.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from agent_factory import ingestion_api  # noqa: F401  -- canonicalizes the hooks modules first

import _praxis  # noqa: E402
import _ticket_state as ts  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"
_HEREDOC_RE = re.compile(r"<<'PYEOF'[^\n]*\n(.*?)\n *PYEOF\n", re.S)


def _block(marker: str) -> str:
    hits = [b for b in _HEREDOC_RE.findall(SCRIPT.read_text()) if marker in b]
    assert len(hits) == 1, f"expected exactly one embedded block containing {marker!r}, got {len(hits)}"
    return hits[0]


def _run(marker: str, argv: list[str], capsys) -> str:
    code = compile(_block(marker), f"<af-ticket-loop:{marker}>", "exec")
    old = sys.argv
    sys.argv = ["-", *argv]
    try:
        exec(code, {"__name__": "__main__"})  # noqa: S102 - running the driver's own source is the point
    except SystemExit:
        pass
    finally:
        sys.argv = old
    return capsys.readouterr().out.strip()


def _ticket(rid: str, **meta) -> dict:
    """A requirement fact. NOTE the default: no ``build_state`` key at all, which is what a
    freshly-blessed ticket actually looks like — not ``build_state=None``, absent."""
    return {"id": f"cid-{rid}", "cid": f"cid-{rid}", "meta": {"requirement_id": rid, **meta}}


@pytest.fixture
def plan(monkeypatch):
    """A blessed plan exactly as intake leaves it: 21 tickets, not one of them stamped."""
    facts = [_ticket(f"T{i}") for i in range(1, 22)]
    monkeypatch.setattr(_praxis, "facts_by", lambda **kw: list(facts))
    monkeypatch.setattr(ts, "parked_on_manual", lambda *a, **k: False)
    return facts


def test_a_freshly_blessed_plan_is_not_reported_as_drained(plan, capsys):
    """THE REGRESSION: 21 unstamped tickets used to count as 0."""
    assert _run("ts.owes_work", ["sports_analysis"], capsys) == "21"


def test_a_just_dispatched_round_is_not_already_closed(plan, capsys):
    """batch_open over unstamped tickets: the round has 3 open, not 0."""
    assert _run("ts.is_open_state", ["sports_analysis", "T1", "T2", "T3"], capsys) == "3"


def test_terminal_states_still_close(plan, monkeypatch, capsys):
    """The fix must not make tickets immortal — finished and blocked still count as done."""
    facts = [
        _ticket("T1", build_state="finished"),
        _ticket("T2", build_state="blocked"),
        _ticket("T3", build_state="in_progress"),
        _ticket("T4", build_state="incomplete"),
        _ticket("T5"),
    ]
    monkeypatch.setattr(_praxis, "facts_by", lambda **kw: list(facts))

    assert _run("ts.owes_work", ["sports_analysis"], capsys) == "3"
    assert _run("ts.is_open_state", ["sports_analysis", "T1", "T2"], capsys) == "0"
    assert _run("ts.is_open_state", ["sports_analysis", "T3", "T4", "T5"], capsys) == "3"


def test_the_two_spellings_of_the_predicate_agree(plan):
    """``claimable`` and the dependency frontier must not disagree about the same ticket.

    They did: ``ready_tickets`` excluded the terminal states (so a fresh ticket was READY) while
    ``claimable`` required membership in the non-terminal list (so the same ticket was invisible).
    A frontier full of work behind a count of zero is how the loop justified exiting.
    """
    assert len(ts.ready_tickets(plan)) == 21
    assert sum(1 for f in plan if ts.owes_work(f)) == 21


@pytest.mark.parametrize(
    ("state", "open_"),
    [(None, True), ("", True), ("   ", True), ("incomplete", True), ("in_progress", True),
     ("finished", False), ("blocked", False)],
)
def test_is_open_state_table(state, open_):
    assert ts.is_open_state(state) is open_
