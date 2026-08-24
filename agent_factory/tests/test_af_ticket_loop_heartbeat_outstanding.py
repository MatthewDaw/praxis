"""A stall warning must name the tickets that are actually still out.

`af_round_heartbeat` takes, by its own signature, `<outstanding ids>`. It was being handed
`$ids_csv` — the WHOLE round — so every heartbeat named every ticket regardless of how many had
finished. Observed 2026-08-24, praxis round #3, two consecutive lines:

    round #3 progress: 3/4 finished
    round #3 still working — 30min quiet ... Still outstanding: R4c,R4b,R3a,R1b.

The count and the list in the same message disagree. On the STALL WARNING path that is the line an
operator acts on, and it sends them to look at three tickets that are already done — while the one
that is genuinely stuck is buried among them. A report that names the wrong thing is worse than no
report, because it is acted upon.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from agent_factory import ingestion_api  # noqa: F401  -- canonicalizes the hooks modules first

import _praxis  # noqa: E402
import _ticket_state as ts  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"
_HEREDOC_RE = re.compile(r"<<'PYEOF'[^\n]*\n(.*?)\n *PYEOF\n", re.S)


def _block(marker: str) -> str:
    hits = [b for b in _HEREDOC_RE.findall(SCRIPT.read_text()) if marker in b]
    assert len(hits) == 1, f"expected exactly one block containing {marker!r}, got {len(hits)}"
    return hits[0]


def _run(marker: str, argv: list[str], capsys) -> str:
    code = compile(_block(marker), f"<af-ticket-loop:{marker}>", "exec")
    old = sys.argv
    sys.argv = ["-", *argv]
    try:
        exec(code, {"__name__": "__main__"})  # noqa: S102
    except SystemExit:
        pass
    finally:
        sys.argv = old
    return capsys.readouterr().out.strip()


def _ticket(rid: str, state) -> dict:
    meta = {"requirement_id": rid}
    if state is not None:
        meta["build_state"] = state
    return {"id": f"cid-{rid}", "cid": f"cid-{rid}", "meta": meta}


MARKER = "print(' '.join(sorted(out)))"


def test_only_the_unfinished_tickets_are_named(monkeypatch, capsys):
    """THE REGRESSION, with praxis round #3's exact shape: 3 of 4 done."""
    facts = [
        _ticket("R1b", "finished"),
        _ticket("R2", "finished"),
        _ticket("R4a", "finished"),
        _ticket("R3a", "in_progress"),
    ]
    monkeypatch.setattr(_praxis, "facts_by", lambda **kw: list(facts))

    out = _run(MARKER, ["praxis", "R1b", "R2", "R4a", "R3a"], capsys)

    assert out == "R3a"


def test_a_blocked_ticket_is_not_outstanding(monkeypatch, capsys):
    """Blocked is terminal for the round — it is not work still in flight, and naming it as such
    would send an operator waiting for something that will never move on its own."""
    facts = [_ticket("A", "blocked"), _ticket("B", "in_progress")]
    monkeypatch.setattr(_praxis, "facts_by", lambda **kw: list(facts))

    assert _run(MARKER, ["p", "A", "B"], capsys) == "B"


def test_a_freshly_dispatched_ticket_with_no_state_is_outstanding(monkeypatch, capsys):
    """It has not been claimed yet, so it is the most outstanding thing there is. Same predicate
    the claimable count uses, so the two cannot disagree."""
    facts = [_ticket("A", None), _ticket("B", "finished")]
    monkeypatch.setattr(_praxis, "facts_by", lambda **kw: list(facts))

    assert _run(MARKER, ["p", "A", "B"], capsys) == "A"


def test_nothing_open_reports_nothing(monkeypatch, capsys):
    facts = [_ticket("A", "finished"), _ticket("B", "blocked")]
    monkeypatch.setattr(_praxis, "facts_by", lambda **kw: list(facts))

    assert _run(MARKER, ["p", "A", "B"], capsys) == ""


def test_tickets_outside_the_round_are_not_reported(monkeypatch, capsys):
    facts = [_ticket("A", "in_progress"), _ticket("Z", "in_progress")]
    monkeypatch.setattr(_praxis, "facts_by", lambda **kw: list(facts))

    assert _run(MARKER, ["p", "A"], capsys) == "A"


# ------------------------------------------------------------------------------- the wiring ----

#: Spelled out, because ORDER is what a wiring test is for.
EXACT_HEARTBEAT_CALL = (
    'af_round_heartbeat "$round" "$now/$open" '
    '"$(printf \'%s\' "${hb_open:-$ids_csv}" | tr \' \' \',\')"'
)


def test_the_heartbeat_is_handed_the_open_ids_in_the_right_position() -> None:
    """Membership is not enough, and this file got that wrong first time.

    Verification executed the malformed call `af_round_heartbeat "$now/$open" "$round" "$hb_open"`
    -- argv 1 and argv 2 swapped -- and it satisfied every membership assertion written here. A
    wiring test whose subject is argument ORDER has to assert order.
    """
    src = SCRIPT.read_text()
    call = next(
        line
        for line in src.splitlines()
        if "af_round_heartbeat " in line and not line.strip().startswith("#")
    )
    assert call.strip() == EXACT_HEARTBEAT_CALL, (
        f"  found:    {call.strip()}\n  expected: {EXACT_HEARTBEAT_CALL}")


def test_an_unanswerable_query_widens_the_report_rather_than_emptying_it():
    """If Praxis cannot say which are open, reporting NOTHING outstanding would read as 'the round
    is done' at precisely the moment we cannot tell. Fall back to the full round."""
    src = SCRIPT.read_text()
    assert '${hb_open:-$ids_csv}' in src


def test_the_predicate_matches_the_one_the_counts_use():
    """batch_open returns the tally and batch_open_ids the membership; if they used different
    predicates the count and the list could disagree again, in a subtler way."""
    assert "ts.is_open_state(m.get('build_state'))" in _block(MARKER)
    assert ts.is_open_state(None) is True
