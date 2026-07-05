"""U8 — form-fill seam tests. The golden $40k single session yields the expected lines, a stable
hash, and a receipt naming the defaulted fields; a value with no non-LLM provenance fails (S2)."""

from __future__ import annotations

import pytest

from agent_factory.fulfill.evaluator import evaluate
from agent_factory.fulfill.formfill import (
    ProvenanceError,
    build_line_items,
    produce_deliverable,
)

_GOLDEN_FACTS = {
    "filing_status": "single", "box1_wages": 40000,
    "box2_withholding": 3200, "other_income": 0,
    "employee_name": "Jordan Avery Rivera",
}
# wages + withholding came from the W-2; filing_status from the user; other_income defaulted.
_COVER = {"box1_wages": "w2", "box2_withholding": "w2",
          "filing_status": "user", "other_income": "default"}


def _produce(domain):
    results = evaluate(domain, _GOLDEN_FACTS, mode="final")
    return produce_deliverable(
        domain, results, facts=_GOLDEN_FACTS, cover_sources=_COVER,
        defaulted_fields=["other_income"],
    )


def test_golden_lines_populated(domain):
    d = _produce(domain)
    assert d.line("line_1a_wages")["value"] == 40000
    assert d.line("line_12_standard_deduction")["value"] == 15750
    assert d.line("line_15_taxable_income")["value"] == 24250
    assert d.line("line_16_tax")["value"] == 2672
    assert d.line("line_34_refund")["value"] == 528
    assert d.pdf_bytes[:4] == b"%PDF"
    assert d.source_form == "rendered_1040_shaped"


def test_provenance_tags(domain):
    d = _produce(domain)
    assert d.provenance["line_1a_wages"] == "w2"
    assert d.provenance["line_25a_withholding"] == "w2"
    assert d.provenance["line_8_other_income"] == "default"
    assert d.provenance["line_16_tax"] == "engine"


def test_receipt_lists_defaulted_fields(domain):
    d = _produce(domain)
    assert len(d.receipt) == 1
    r = d.receipt[0]
    assert r["field"] == "other_income"
    assert r["value"] == 0
    assert "no non-W-2 income" in r["justification"]


def test_hash_is_stable(domain):
    a = _produce(domain)
    b = _produce(domain)
    assert a.content_hash == b.content_hash
    assert len(a.content_hash) == 64


def test_hash_changes_with_inputs(domain):
    base = _produce(domain)
    other_facts = {**_GOLDEN_FACTS, "box1_wages": 50000}
    results = evaluate(domain, other_facts, mode="final")
    changed = produce_deliverable(domain, results, facts=other_facts,
                                  cover_sources=_COVER, defaulted_fields=["other_income"])
    assert changed.content_hash != base.content_hash


def test_value_with_no_provenance_fails_assertion(domain):
    results = evaluate(domain, _GOLDEN_FACTS, mode="final")
    bad_cover = {**_COVER, "box1_wages": "llm"}  # the LLM never authors a number
    with pytest.raises(ProvenanceError):
        build_line_items(domain, results, bad_cover)


def test_identity_filing_status_rendered_label(domain):
    d = _produce(domain)
    # the PDF text carries the rendered "Single" label, not the raw enum.
    assert b"Single" in d.pdf_bytes
