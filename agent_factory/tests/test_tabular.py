from agent_factory.tabular import linearize


def test_standard_deduction_table_keeps_every_row_distinct():
    # The real H6 case: in Praxis this table lost the MFJ row to a silent over-merge.
    text = """
| filing_status | std_deduction_2025 |
| --- | --- |
| Single | 15750 |
| Married filing separately | 15750 |
| Head of household | 23625 |
| Married filing jointly | 31500 |
""".strip()
    result = linearize(text)
    assert len(result.facts) == 4
    # Every row survives as its own distinct, self-contained fact.
    assert 'For filing_status "Single", std_deduction_2025 is 15750.' in result.facts
    assert 'For filing_status "Married filing jointly", std_deduction_2025 is 31500.' in result.facts
    # The two $15,750 rows are NOT collapsed despite identical values.
    assert sum("15750" in f for f in result.facts) == 2
    assert not result.residual_prose


def test_multi_column_table_emits_one_fact_per_cell():
    text = """
| role | can_edit_themes | can_manage_roster |
|------|-----------------|-------------------|
| coach | yes | yes |
| captain | no | no |
""".strip()
    result = linearize(text)
    # 2 rows x 2 attribute columns = 4 atomic facts (the same-subject/different-attribute
    # shape the subject-only dedup guard would miss).
    assert len(result.facts) == 4
    assert 'For role "coach", can_edit_themes is yes.' in result.facts
    assert 'For role "coach", can_manage_roster is yes.' in result.facts


def test_key_value_block():
    text = "name: daily_prompt\nrequired: true\nresponse_type: text_short"
    result = linearize(text)
    assert result.facts == [
        "The name is daily_prompt.",
        "The required is true.",
        "The response_type is text_short.",
    ]


def test_prose_passes_through_as_residual():
    text = "This is a normal paragraph describing the feature.\nIt has no tables."
    result = linearize(text)
    assert result.facts == []
    assert "normal paragraph" in result.residual_prose


def test_single_key_value_line_is_treated_as_prose():
    # One key:value line is too weak a signal — leave it as prose, don't fragment.
    text = "Note: remember to verify the build"
    result = linearize(text)
    assert result.facts == []
    assert result.residual_prose == text


def test_table_and_prose_mixed():
    text = """
Some intro prose about deductions.

| filing_status | amount |
| --- | --- |
| Single | 15750 |
""".strip()
    result = linearize(text)
    assert result.facts == ['For filing_status "Single", amount is 15750.']
    assert "intro prose" in result.residual_prose
