"""Loss point A: distillation must not under-emit tabular/templated input.

The reproduction gate (``test_reproduce_*``) is built from a *real* table in the
first PRD's Roles & Permissions section (agent_factory inspiration), not a
synthetic one. It demonstrates and quantifies current under-emission: a
summarizing LLM collapses sibling rows, and the offline sentence splitter
mangles the table. The remaining tests pin the deterministic linearizer and the
two wired call sites.
"""

from __future__ import annotations

from knowledge.injestion.injestor_variants.prompt_injestor import (
    PromptIngestor,
    segment_passthrough,
)
from knowledge.injestion.table_linearizer import linearize_table

# ---------------------------------------------------------------------------
# Real PRD fixtures (Roles & Permissions, agent_factory first PRD).
#
# The PRD states three roles, each a "key: value" row, plus a recommended
# permission-rules block. Below is the same content as the canonical tabular
# forms the proposal names. role x permission is the "same subject varies by
# attribute is not the issue here; subject varies per row" field->value shape,
# and the markdown table is the role x permission shape.
# ---------------------------------------------------------------------------

# As repeated ``key: value`` rows (verbatim shape from the PRD's "Roles" list).
PRD_ROLES_KEY_VALUE = """\
Athlete: completes daily rep + checklist + ratings; sees team participation %
Captain/Leader: same as athlete + can post leader message
Coach/Admin: creates weekly themes, daily prompts, team habits checklist, sees dashboards
"""

# The PRD's role x permission content as a markdown table (same-subject-ish rows
# that differ only by a key -- exactly the collapse-prone shape).
PRD_PERMISSIONS_MD = """\
| role | scope | can_post_message | can_view_individual_compliance |
| --- | --- | --- | --- |
| athlete | team | no | no |
| captain | team | yes | no |
| coach | team | yes | yes |
"""


def _summarizing_llm(prompt: str) -> str:
    """A realistic distiller that collapses rows sharing a sentence shape.

    This is the documented failure: given a permissions table the model emits a
    short summary instead of one line per row. It is deliberately lossy so the
    reproduction test fails against the *old* (prompt-only) code path.
    """
    if "role" in prompt and "compliance" in prompt:
        return (
            "Roles have different permissions in the team.\n"
            "Coaches have the most access."
        )
    # For non-table prose, behave like a faithful one-idea-per-line splitter.
    body = prompt.split("INPUT:\n", 1)[-1]
    return body


# ---------------------------------------------------------------------------
# Reproduction gate.
# ---------------------------------------------------------------------------


def test_reproduce_llm_path_emits_one_fact_per_row():
    """N distinct rows -> N distinct facts on the LLM path (was < N before)."""
    rows = [ln for ln in PRD_PERMISSIONS_MD.splitlines() if ln.strip()][2:]
    n_rows = len(rows)
    assert n_rows == 3

    insights = PromptIngestor(graph=None, llm=_summarizing_llm).synthesis(
        PRD_PERMISSIONS_MD
    )

    # Before the fix, the summarizing LLM emitted 2 facts for 3 rows.
    assert len(insights) == n_rows, (
        f"under-emission: {len(insights)} facts for {n_rows} rows"
    )
    # Each fact is lexically distinct and names its own row subject.
    texts = [i.raw_text for i in insights]
    assert len(set(texts)) == n_rows
    assert any("athlete" in t for t in texts)
    assert any("captain" in t for t in texts)
    assert any("coach" in t for t in texts)


def test_reproduce_offline_path_does_not_mangle_table():
    """The offline path emits one fact per row instead of sentence-mangling."""
    rows = [ln for ln in PRD_PERMISSIONS_MD.splitlines() if ln.strip()][2:]
    insights = PromptIngestor(graph=None, llm=None).synthesis(PRD_PERMISSIONS_MD)
    assert len(insights) == len(rows) == 3
    assert all("For the" in i.raw_text for i in insights)


# ---------------------------------------------------------------------------
# Linearizer unit behavior (liftable pure function).
# ---------------------------------------------------------------------------


def test_markdown_table_folds_identity_into_text():
    facts = linearize_table(PRD_PERMISSIONS_MD)
    assert len(facts) == 3
    # Boolean cells normalized to true/false, identity folded in.
    assert "For the athlete role, scope is team, can_post_message = false" in facts[0]
    assert "can_post_message = true" in facts[1]
    assert "can_view_individual_compliance = true" in facts[2]
    # No bare "key: value" leakage for table cells.
    assert not any(": no" in f or ": yes" in f for f in facts)


def test_key_value_block_is_detected_and_distinct():
    facts = linearize_table(PRD_ROLES_KEY_VALUE)
    assert len(facts) == 3
    assert facts[0].startswith("Athlete:")
    assert facts[1].startswith("Captain/Leader:")
    assert facts[2].startswith("Coach/Admin:")
    assert len(set(facts)) == 3


def test_csvish_headered_rows_linearize():
    csv = "field,required\nemail,yes\nphone,no\nname,yes\n"
    facts = linearize_table(csv)
    assert len(facts) == 3
    assert "For the email field, required = true" in facts[0]
    assert "required = false" in facts[1]


def test_field_required_shape_subject_varies_per_row():
    md = (
        "| field | required |\n"
        "| --- | --- |\n"
        "| daily_prompt | yes |\n"
        "| theme | no |\n"
    )
    facts = linearize_table(md)
    assert facts == [
        "For the daily_prompt field, required = true",
        "For the theme field, required = false",
    ]


# ---------------------------------------------------------------------------
# No-regression: prose must NOT be treated as a table.
# ---------------------------------------------------------------------------


def test_prose_is_not_tabular():
    prose = (
        "Alessandro Volta was an Italian physicist. He invented the electric "
        "battery in 1800. His work was foundational to electrochemistry."
    )
    assert linearize_table(prose) == []


def test_single_stray_colon_is_not_a_table():
    prose = "Note: the deadline is Friday. Everyone should review the draft beforehand."
    assert linearize_table(prose) == []


def test_prose_falls_through_to_sentence_split_offline():
    prose = "Volta invented the battery. He was Italian."
    facts = segment_passthrough(prose)
    assert facts == ["Volta invented the battery.", "He was Italian."]
