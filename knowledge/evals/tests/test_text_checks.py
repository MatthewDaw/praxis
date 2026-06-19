"""Tests for the reusable text deterministic checks."""

from knowledge.evals.deterministic_checks.text import (
    forbids_substring,
    is_empty,
    json_valid,
    max_line_length,
    occurs_at_most,
    ordered_substrings,
    regex_absent,
    regex_matches,
    requires_all_substrings,
)
from knowledge.evals.eval_def import EvalContext


def _ctx(text: str) -> EvalContext:
    return EvalContext(case_id="c", output=text)


def test_forbids_substring_pass():
    assert forbids_substring(_ctx("hello world"), text="xyz").passed


def test_forbids_substring_fail():
    assert not forbids_substring(_ctx("hello world"), text="world").passed


def test_forbids_substring_case_insensitive_fail():
    res = forbids_substring(_ctx("Hello World"), text="world", case_insensitive=True)
    assert not res.passed


def test_forbids_substring_case_sensitive_pass():
    assert forbids_substring(_ctx("Hello World"), text="world").passed


def test_requires_all_substrings_pass():
    assert requires_all_substrings(_ctx("a b c"), texts=["a", "c"]).passed


def test_requires_all_substrings_fail():
    res = requires_all_substrings(_ctx("a b c"), texts=["a", "z"])
    assert not res.passed
    assert "z" in res.evidence


def test_max_line_length_pass():
    assert max_line_length(_ctx("short\nlines\n"), limit=10).passed


def test_max_line_length_fail():
    res = max_line_length(_ctx("ok\nthis line is way too long"), limit=5)
    assert not res.passed
    assert "longest" in res.evidence


def test_occurs_at_most_pass():
    assert occurs_at_most(_ctx("ab ab"), text="ab", n=2).passed


def test_occurs_at_most_fail():
    res = occurs_at_most(_ctx("ab ab ab"), text="ab", n=2)
    assert not res.passed
    assert "3" in res.evidence


def test_ordered_substrings_pass():
    assert ordered_substrings(_ctx("first then second"), texts=["first", "second"]).passed


def test_ordered_substrings_fail_order():
    res = ordered_substrings(_ctx("second then first"), texts=["first", "second"])
    assert not res.passed


def test_ordered_substrings_fail_missing():
    assert not ordered_substrings(_ctx("only first"), texts=["first", "second"]).passed


def test_regex_matches_pass():
    assert regex_matches(_ctx("id=42"), pattern=r"id=\d+").passed


def test_regex_matches_fail():
    assert not regex_matches(_ctx("id=abc"), pattern=r"id=\d+").passed


def test_regex_absent_pass():
    assert regex_absent(_ctx("clean"), pattern=r"secret").passed


def test_regex_absent_fail():
    assert not regex_absent(_ctx("a secret here"), pattern=r"secret").passed


def test_json_valid_pass():
    assert json_valid(_ctx('  {"a": 1}  ')).passed


def test_json_valid_pass_fenced():
    assert json_valid(_ctx('```json\n{"a": 1}\n```')).passed


def test_json_valid_fail():
    assert not json_valid(_ctx("not json {")).passed


def test_is_empty_pass():
    assert is_empty(_ctx("   \n  ")).passed


def test_is_empty_fail():
    assert not is_empty(_ctx("x")).passed
