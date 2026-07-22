"""U1: the file-backed seeded-check library and its loader."""

from __future__ import annotations

import textwrap

import pytest

from agent_factory.seeded_checks import GRADED, load_seeded_checks, universal_seeded_checks


def _write(tmp_path, body: str):
    p = tmp_path / "seeded_checks.toml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_ships_valid_default_library():
    """The real shipped library parses and every graded check has axes."""
    checks = load_seeded_checks()
    assert checks, "expected a non-empty default library"
    ids = {c.check_id for c in checks}
    assert {"correctness-review", "security-review", "error-paths-covered"} <= ids
    for c in checks:
        if c.kind == GRADED:
            assert c.rubric is not None and c.rubric.axes


def test_parses_graded_and_binary(tmp_path):
    p = _write(tmp_path, """
        [[check]]
        check_id = "bin-one"
        kind = "binary"
        run = "true"

        [[check]]
        check_id = "grade-one"
        kind = "graded"
        applies_to = ["auth"]
        confidence_floor = 6
        [[check.axes]]
        name = "logic"
        threshold = 0.7
    """)
    checks = {c.check_id: c for c in load_seeded_checks(p)}
    assert checks["bin-one"].kind == "binary" and checks["bin-one"].run == "true"
    g = checks["grade-one"]
    assert g.kind == "graded" and g.applies_to == ("auth",)
    assert g.rubric.confidence_floor == 6 and g.rubric.axes[0].name == "logic"


def test_defaults_applies_to_wildcard(tmp_path):
    p = _write(tmp_path, """
        [[check]]
        check_id = "b"
        kind = "binary"
        run = "true"
    """)
    assert load_seeded_checks(p)[0].applies_to == ("*",)


def test_duplicate_check_id_rejected(tmp_path):
    p = _write(tmp_path, """
        [[check]]
        check_id = "dup"
        run = "true"
        [[check]]
        check_id = "dup"
        run = "false"
    """)
    with pytest.raises(ValueError, match="duplicate check_id.*dup"):
        load_seeded_checks(p)


def test_binary_without_run_rejected(tmp_path):
    p = _write(tmp_path, """
        [[check]]
        check_id = "b"
        kind = "binary"
    """)
    with pytest.raises(ValueError, match="binary check requires a run"):
        load_seeded_checks(p)


def test_graded_without_axes_rejected(tmp_path):
    p = _write(tmp_path, """
        [[check]]
        check_id = "g"
        kind = "graded"
    """)
    with pytest.raises(ValueError, match="at least one axis"):
        load_seeded_checks(p)


def test_axis_threshold_out_of_range_rejected(tmp_path):
    p = _write(tmp_path, """
        [[check]]
        check_id = "g"
        kind = "graded"
        [[check.axes]]
        name = "x"
        threshold = 1.5
    """)
    with pytest.raises(ValueError, match="threshold must be in"):
        load_seeded_checks(p)


def test_ships_minimalism_dry_universal():
    """U5: the always-on ``minimalism-dry`` graded check ships report-only, in the universal lane,
    with strict axes and literal good/slop anchors demonstrating strict minimization."""
    checks = {c.check_id: c for c in load_seeded_checks()}
    m = checks["minimalism-dry"]
    assert m.kind == GRADED and m.applies_to == ("*",)
    assert m.promote_universal is True and m.report_only is True  # ships report-only (calibration)
    assert {a.name for a in m.rubric.axes} == {"minimalism", "deduplication", "dry"}
    assert m.rubric.anchors is not None
    # >=3 anchors demonstrating strict minimization (dead-code/speculative vs minimal; copy-paste vs DRY).
    assert len(m.rubric.anchors.good) >= 3 and len(m.rubric.anchors.slop) >= 3
    # It is the (only) member of the universal lane the library ships today.
    assert "minimalism-dry" in {c.check_id for c in universal_seeded_checks(list(checks.values()))}


def test_minimalism_dry_prompt_embeds_anchors_verbatim():
    from agent_factory.graded_verdict import build_judge_prompt
    m = {c.check_id: c for c in load_seeded_checks()}["minimalism-dry"]
    prompt = build_judge_prompt(m.rubric, "DIFF")
    assert "CALIBRATION" in prompt
    for snippet in (*m.rubric.anchors.good, *m.rubric.anchors.slop):
        assert snippet in prompt  # verbatim — the reproducibility claim


def test_adding_one_entry_is_the_only_edit(tmp_path):
    """Extending the library is a single appended block — asserted by loading a fixture with an
    extra record and seeing exactly one more check, no code path change."""
    base = """
        [[check]]
        check_id = "a"
        run = "true"
    """
    extended = base + """
        [[check]]
        check_id = "b"
        run = "true"
    """
    assert len(load_seeded_checks(_write(tmp_path, base))) == 1
    assert {c.check_id for c in load_seeded_checks(_write(tmp_path, extended))} == {"a", "b"}
