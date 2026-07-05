"""U9 (loop) — the gather orchestrator, driven against the in-memory FakeBackend Praxis.

The golden end-to-end session (paste W-2 -> one filing-status question -> completed 1040, $528
refund, 1-line receipt); the budget refusal; the guardrail refusals; the recompute-on-correction;
and the completeness gate (producing before completeness is refused).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_factory.fulfill.loop import Conversation, ProduceBeforeComplete
from agent_factory.fulfill.session import space_id_for, start_session
from tests.fulfill.conftest import FakeFulfillPraxis

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_W2 = (FIXTURES / "sample_w2.txt").read_text(encoding="utf-8")


def _conv(domain, backend, sid="golden"):
    client = FakeFulfillPraxis(backend, space=space_id_for(sid))
    session = start_session(domain, sid, client=client)
    return Conversation(session)


def test_golden_end_to_end(domain, backend):
    conv = _conv(domain, backend)

    # Turn 1: paste the W-2 -> wages/withholding covered from the doc (0 questions), readback
    # auto-confirmed, other_income defaulted, then filing_status asked once (highest materiality).
    t1 = conv.handle_turn(SAMPLE_W2)
    assert t1.deliverable is None
    assert t1.question is not None and "filing status" in t1.question.lower()
    assert conv.budget.asked == 1
    assert conv.cover_sources["box1_wages"] == "w2"

    # Turn 2: answer filing status -> completeness hits 0 -> the 1040 is produced.
    t2 = conv.handle_turn("single")
    assert t2.done and t2.deliverable is not None
    d = t2.deliverable
    assert d.line("line_34_refund")["value"] == 528
    assert d.content_hash and len(d.content_hash) == 64
    # exactly one assumption-receipt line: other_income defaulted.
    assert [r["field"] for r in d.receipt] == ["other_income"]
    # confirmed_w2 was covered by the readback, not a budgeted question.
    assert conv.budget.asked == 1


def test_budget_refusal_defaults_remaining(domain, backend):
    conv = _conv(domain, backend, "tight")
    conv.budget.max = 0  # no questions allowed at all
    t1 = conv.handle_turn(SAMPLE_W2)
    # the ask is hard-refused; filing_status falls back to its default and the form still produces.
    assert any(e.type == "budget_exhausted" for e in conv.trace)
    assert t1.question is None
    assert t1.done and t1.deliverable is not None
    fields = {r["field"] for r in t1.deliverable.receipt}
    assert "filing_status" in fields and "other_income" in fields


def test_guardrail_1099_refusal_fabricates_nothing(domain, backend):
    conv = _conv(domain, backend, "g1")
    t = conv.handle_turn("Attached is my 1099-NEC from freelance work, please file it")
    assert t.refusal is not None
    assert t.deliverable is None
    assert any(e.type == "guardrail_refusal" and e.data["rule_id"] == "OUT_OF_SCOPE_1099"
               for e in conv.trace)


def test_guardrail_advice_refusal(domain, backend):
    conv = _conv(domain, backend, "g2")
    t = conv.handle_turn("what's the best way to minimize my taxes?")
    assert t.refusal is not None
    assert any(e.data.get("rule_id") == "SCOPE_NO_ADVICE" for e in conv.trace)


def test_pii_is_redacted_not_refused(domain, backend):
    conv = _conv(domain, backend, "pii")
    # a bare SSN with no W-2 content: redacted, the turn continues (no refusal), still asks.
    t = conv.handle_turn("my ssn is 123-45-6789, I'm filing")
    assert t.refusal is None
    assert any(e.type == "pii_redacted" for e in conv.trace)


def test_correction_recomputes_not_stale(domain, backend):
    conv = _conv(domain, backend, "fix")
    conv.handle_turn(SAMPLE_W2)
    conv.handle_turn("single")
    first = conv.deliverable
    assert first.line("line_1a_wages")["value"] == 40000

    corrected = SAMPLE_W2.replace("40,000.00", "50,000.00")
    t = conv.handle_turn(corrected)
    assert t.deliverable is not None
    assert t.deliverable.line("line_1a_wages")["value"] == 50000
    assert t.deliverable.content_hash != first.content_hash  # recomputed, not stale


def test_produce_before_complete_is_refused(domain, backend):
    conv = _conv(domain, backend, "early")
    with pytest.raises(ProduceBeforeComplete):
        conv.produce()


def test_filing_status_not_silently_defaulted_before_wages(domain, backend):
    # Regression: if the FIRST turn is not a W-2 (a greeting), filing_status materiality is
    # unmeasurable (wages unknown) — it must be ASKED, never silently defaulted to single.
    conv = _conv(domain, backend, "greet")
    t = conv.handle_turn("hi, I'd like to do my taxes")
    assert t.question is not None
    assert conv.pending_ask is not None and conv.pending_ask.field == "filing_status"
    # filing_status was NOT closed by default.
    assert "filing_status" not in conv.defaulted_fields
    assert conv.facts.get("filing_status") is None


def test_invariant_violation_blocks_production(domain, backend):
    # withholding > wages passes each per-field schema but violates the cross-field invariant;
    # the loop must refuse to produce a deliverable from that state.
    conv = _conv(domain, backend, "bad")
    bad_w2 = SAMPLE_W2.replace("3,200.00", "90,000.00")  # box 2 now exceeds box 1 (40,000)
    conv.handle_turn(bad_w2)
    t = conv.handle_turn("single")
    assert t.deliverable is None
    assert t.refusal is not None
    assert any(e.type == "invariant_violation" for e in conv.trace)


def test_trace_emits_typed_events(domain, backend):
    conv = _conv(domain, backend, "trace")
    conv.handle_turn(SAMPLE_W2)
    conv.handle_turn("single")
    types = {e.type for e in conv.trace}
    assert {"document_extracted", "question_asked", "requirement_covered",
            "deliverable_produced"} <= types
