"""U7 — document-extraction seam tests. The sample W-2 yields the expected fields; partial/empty
inputs degrade gracefully (no crash)."""

from __future__ import annotations

from pathlib import Path

from agent_factory.fulfill.extract import W2Extractor, extractor_for

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_W2 = (FIXTURES / "sample_w2.txt").read_text(encoding="utf-8")


def test_sample_w2_extracts_expected_fields(domain):
    result = extractor_for(domain).extract(SAMPLE_W2)
    assert result.fields["box1_wages"] == 40000.0  # Box 1, not Box 3/5 (41,500)
    assert result.fields["box2_withholding"] == 3200.0
    assert "Brightline Coffee Roasters" in result.fields["employer"]
    assert result.fields["employee_name"] == "Jordan Avery Rivera"
    assert not result.unreadable


def test_picks_box1_not_box3(domain):
    # explicit guard: Box 3/5 social-security wages (41,500) must not be read as wages.
    result = extractor_for(domain).extract(SAMPLE_W2)
    assert result.fields["box1_wages"] != 41500.0


def test_missing_box2_leaves_it_unknown(domain):
    messy = "\n".join(line for line in SAMPLE_W2.splitlines() if "Box 2 " not in line and "Box 2  " not in line)
    result = W2Extractor(domain).extract(messy)
    assert result.fields["box1_wages"] == 40000.0
    assert "box2_withholding" not in result.fields  # asked later, not crashed


def test_empty_document_is_structured_not_exception(domain):
    result = W2Extractor(domain).extract("")
    assert result.unreadable
    assert result.fields == {}
    assert result.notes


def test_unrecognizable_text_returns_no_fields(domain):
    result = W2Extractor(domain).extract("this is just a grocery list: milk, eggs, bread")
    assert result.fields == {}
    assert not result.unreadable
    assert result.notes
