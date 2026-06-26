"""Tests for the filing-status identity helper used by the dedup/conflict steps."""

from __future__ import annotations

from knowledge.knowledge_graph.write_policy.write_step_variants.filing_status import (
    bracket_rate,
    different_status,
    distinct_tax_facts,
    dominant_filing_status,
)

# Real distilled-fact shapes: whole-ladder blocks dominated by one status.
_SINGLE = (
    "TY2025 ordinary income tax brackets for single filers: 10% up to $11,925; "
    "single filers are taxed at 22% on income between $48,475 and $103,350."
)
# The MFS block name-drops Single in passing — dominance, not mere presence, decides.
_MFS = (
    "The 2025 tax-rate schedule for Married Filing Separately (MFS). The MFS "
    "thresholds match the Single filing status except in the top two brackets. The "
    "third tax bracket for MFS is 22% on the amount over $48,475 up to $103,350."
)
_MFJ = "TY2025 brackets, Married filing jointly: 22% on $96,950-$206,700."
_HOH = "Head of household 22% bracket is $64,850-$103,350 for TY2025."
# A summary table listing every status once -> ambiguous (tie) -> no dominant status.
_COMBINED = (
    "Standard deduction: Single $15,750; Married filing jointly $31,500; "
    "Married filing separately $15,750; Head of household $23,625."
)


def test_dominant_status_picks_the_plurality():
    assert dominant_filing_status(_SINGLE) == "single"
    assert dominant_filing_status(_MFS) == "mfs"  # not "single" despite the mention
    assert dominant_filing_status(_MFJ) == "mfj"
    assert dominant_filing_status(_HOH) == "hoh"


def test_no_status_and_ties_are_none():
    assert dominant_filing_status("Line 9 is total income; Line 11 is AGI.") is None
    assert dominant_filing_status(_COMBINED) is None  # 4-way tie -> ambiguous


def test_paraphrased_jointly_separately_resolve():
    # The distiller writes "married couples filing jointly", not "married filing
    # jointly" — the looser "filing jointly"/"filing separately" forms must resolve.
    assert dominant_filing_status(
        "Married couples filing jointly face a 22% tax rate on $96,950-$206,700."
    ) == "mfj"
    assert dominant_filing_status(
        "Married individuals filing separately are taxed at 22% on $48,475-$103,350."
    ) == "mfs"


def test_different_status_only_fires_when_both_unambiguous():
    assert different_status(_SINGLE, _MFS) is True
    assert different_status(_SINGLE, _MFJ) is True
    assert different_status(_SINGLE, _SINGLE) is False  # same status
    assert different_status(_SINGLE, _COMBINED) is False  # one side ambiguous
    assert different_status(_COMBINED, _MFJ) is False


# --- Bracket identity (status, rate) -----------------------------------------

_S22 = "Single filers are taxed at a 22% rate on income between $48,475 and $103,350."
_S24 = "The 24% tax rate for single filers applies to income between $103,350 and $197,300."
_MFS22 = "Married individuals filing separately: 22% on income over $48,475 up to $103,350."
_MFJ22 = "Married couples filing jointly face a 22% tax rate on income between $96,950 and $206,700."


def test_bracket_rate_single_only():
    assert bracket_rate(_S22) == "22"
    assert bracket_rate(_SINGLE) is None  # whole-ladder block names several rates
    assert bracket_rate("Line 9 is total income.") is None  # no rate


def test_distinct_tax_facts_holds_brackets_apart():
    # Same status, adjacent rungs -> distinct (the within-status false-contradiction).
    assert distinct_tax_facts(_S22, _S24) is True
    # Same rate, different status, identical range -> distinct (the cross-status merge).
    assert distinct_tax_facts(_S22, _MFS22) is True
    # Same rate, different status, different range -> distinct (the false clash).
    assert distinct_tax_facts(_S22, _MFJ22) is True
    # The same bracket restated -> NOT distinct (ordinary dedup still applies).
    assert distinct_tax_facts(_S22, "Single 22% covers $48,475 to $103,350.") is False
    # Standard-deduction twins fall back to filing-status identity.
    assert distinct_tax_facts(
        "Standard deduction for Single filers is $15,750.",
        "Standard deduction for married filing separately is $15,750.",
    ) is True
