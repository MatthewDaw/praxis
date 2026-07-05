"""U3 — field validator tests. Valid inputs pass; each invalid fixture returns a structured error
naming the field; the cross-field invariant rejects withholding > wages."""

from __future__ import annotations

from agent_factory.fulfill.validate import validate_cross_field, validate_field


def test_valid_enum_and_number(domain):
    assert validate_field(domain, "filing_status", "single").ok
    r = validate_field(domain, "box1_wages", 40000)
    assert r.ok and r.value == 40000.0


def test_negative_wages_rejected(domain):
    r = validate_field(domain, "box1_wages", -5)
    assert not r.ok
    assert r.field == "box1_wages"
    assert "minimum" in r.reason


def test_bad_enum_rejected(domain):
    r = validate_field(domain, "filing_status", "martian")
    assert not r.ok and r.field == "filing_status"


def test_cross_field_withholding_exceeds_wages(domain):
    facts = {"box1_wages": 40000, "box2_withholding": 50000}
    r = validate_cross_field(domain, facts)
    assert not r.ok
    assert "withholding" in r.reason.lower() or "wages" in r.reason.lower()


def test_cross_field_ok_when_valid(domain):
    assert validate_cross_field(domain, {"box1_wages": 40000, "box2_withholding": 3200}).ok


def test_cross_field_skipped_when_operand_missing(domain):
    # only one operand present -> nothing to violate.
    assert validate_cross_field(domain, {"box1_wages": 40000}).ok


def test_integer_out_of_range_rejected(domain):
    r = validate_field(domain, "dependents", 21)
    assert not r.ok and "maximum" in r.reason


def test_integer_non_whole_rejected(domain):
    r = validate_field(domain, "dependents", 2.5)
    assert not r.ok


def test_overlong_string_rejected(domain):
    r = validate_field(domain, "employer", "x" * 201)
    assert not r.ok and "max_length" in r.reason


def test_unknown_field_rejected(domain):
    r = validate_field(domain, "not_a_field", 1)
    assert not r.ok and "unknown" in r.reason


def test_boolean_coercion(domain):
    assert validate_field(domain, "confirmed_w2", True).value is True
    assert validate_field(domain, "confirmed_w2", "yes").value is True
    assert not validate_field(domain, "confirmed_w2", "maybe").ok


def test_number_accepts_comma_string(domain):
    r = validate_field(domain, "box1_wages", "40,000")
    assert r.ok and r.value == 40000.0
