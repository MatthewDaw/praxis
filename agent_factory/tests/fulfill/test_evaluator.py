"""U2 — evaluator tests.

The oracle: an INDEPENDENT reference implementation of the TY2025 1040 (mirroring
``app/tax_engine.py``) computes expected lines for a grid of (filing_status, wages, withholding);
the data-driven evaluator must match byte-for-byte. The documented golden ($40k single -> taxable
24250, tax 2672, refund 528) is also asserted explicitly. Plus: basis propagation, what_if deltas,
clamping, and determinism.
"""

from __future__ import annotations

import math

import pytest

from agent_factory.fulfill.evaluator import bottom_line, evaluate

# --- independent oracle (NOT the module under test) -------------------------
_STD = {
    "single": 15_750,
    "married_filing_jointly": 31_500,
    "married_filing_separately": 15_750,
    "head_of_household": 23_625,
}
_BRACKETS = {
    "single": [(11925, .10), (48475, .12), (103350, .22), (197300, .24),
               (250525, .32), (626350, .35), (math.inf, .37)],
    "married_filing_jointly": [(23850, .10), (96950, .12), (206700, .22), (394600, .24),
                               (501050, .32), (751600, .35), (math.inf, .37)],
    "married_filing_separately": [(11925, .10), (48475, .12), (103350, .22), (197300, .24),
                                  (250525, .32), (375800, .35), (math.inf, .37)],
    "head_of_household": [(17000, .10), (64850, .12), (103350, .22), (197300, .24),
                          (250500, .32), (626350, .35), (math.inf, .37)],
}


def _oracle(fs: str, wages: float, withholding: float, other: float = 0.0) -> dict:
    total = wages + other
    agi = total
    taxable = max(0.0, agi - _STD[fs])
    tax = 0.0
    lower = 0.0
    for upper, rate in _BRACKETS[fs]:
        if taxable <= lower:
            break
        tax += (min(taxable, upper) - lower) * rate
        lower = upper
    tax = round(tax)
    refund = max(0.0, withholding - tax)
    owed = max(0.0, tax - withholding)
    return {"taxable": taxable, "tax": tax, "refund": refund, "owed": owed, "agi": agi}


def _lines(domain, facts):
    r = evaluate(domain, facts, mode="final")
    return {k: v["value"] for k, v in r.items()}


def test_golden_40k_single(domain):
    facts = {"filing_status": "single", "box1_wages": 40000,
             "box2_withholding": 3200, "other_income": 0}
    lines = _lines(domain, facts)
    assert lines["line_15_taxable_income"] == 24250
    assert lines["line_16_tax"] == 2672
    assert lines["line_34_refund"] == 528
    assert lines["line_37_amount_owed"] == 0
    assert bottom_line(domain, evaluate(domain, facts)) == 528


@pytest.mark.parametrize("fs", list(_STD))
@pytest.mark.parametrize("wages", [0, 12000, 40000, 95000, 210000, 700000])
def test_matches_oracle_across_grid(domain, fs, wages):
    withholding = round(wages * 0.08)
    facts = {"filing_status": fs, "box1_wages": wages,
             "box2_withholding": withholding, "other_income": 0}
    lines = _lines(domain, facts)
    exp = _oracle(fs, wages, withholding)
    assert lines["line_15_taxable_income"] == exp["taxable"]
    assert lines["line_16_tax"] == exp["tax"]
    assert lines["line_11_agi"] == exp["agi"]
    assert lines["line_34_refund"] == exp["refund"]
    assert lines["line_37_amount_owed"] == exp["owed"]


def test_provisional_basis(domain):
    # Only wages known: line 1a known, line 12 (filing_status default) assumed.
    r = evaluate(domain, {"box1_wages": 40000}, mode="provisional")
    assert r["line_1a_wages"]["basis"] == "known"
    assert r["line_12_standard_deduction"]["basis"] == "assumed"
    # default filing_status is single -> std deduction 15750.
    assert r["line_12_standard_deduction"]["value"] == 15750


def test_what_if_overlay_changes_line(domain):
    base = evaluate(domain, {"box1_wages": 40000}, mode="provisional")
    assert base["line_12_standard_deduction"]["value"] == 15750
    whatif = evaluate(domain, {"box1_wages": 40000}, mode="what_if",
                      overlay={"filing_status": "head_of_household"})
    assert whatif["line_12_standard_deduction"]["value"] == 23625


def test_taxable_clamps_at_zero(domain):
    # deduction exceeds AGI -> taxable income clamps to 0, tax 0.
    lines = _lines(domain, {"filing_status": "single", "box1_wages": 5000,
                            "box2_withholding": 0, "other_income": 0})
    assert lines["line_15_taxable_income"] == 0
    assert lines["line_16_tax"] == 0


def test_unknown_required_input_is_null_not_crash(domain):
    # final mode, filing_status missing and no default applied -> line 12 + dependents unknown.
    r = evaluate(domain, {"box1_wages": 40000, "box2_withholding": 0, "other_income": 0},
                 mode="final")
    assert r["line_12_standard_deduction"]["value"] is None
    assert r["line_12_standard_deduction"]["basis"] == "unknown"
    assert r["line_15_taxable_income"]["value"] is None  # depends on the unknown deduction
    assert r["line_16_tax"]["value"] is None


def test_determinism(domain):
    facts = {"filing_status": "single", "box1_wages": 40000,
             "box2_withholding": 3200, "other_income": 0}
    a = evaluate(domain, facts)
    b = evaluate(domain, facts)
    assert a == b


def test_other_income_flows_to_total(domain):
    lines = _lines(domain, {"filing_status": "single", "box1_wages": 40000,
                            "box2_withholding": 3200, "other_income": 1000})
    assert lines["line_9_total_income"] == 41000
    assert lines["line_15_taxable_income"] == 25250
