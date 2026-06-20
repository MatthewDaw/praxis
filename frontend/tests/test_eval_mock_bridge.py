"""Tests for eval_mock_bridge auto-generation."""

from __future__ import annotations

from eval_mock_bridge import (
    HAND_CRAFTED_EVAL_CASE_IDS,
    generate_eval_candidate_dicts,
    namespace_from_case,
)


def test_generate_skips_hand_crafted_case_ids() -> None:
    rows = generate_eval_candidate_dicts(HAND_CRAFTED_EVAL_CASE_IDS)
    case_ids = {row["evalCaseId"] for row in rows}
    assert HAND_CRAFTED_EVAL_CASE_IDS.isdisjoint(case_ids)


def test_namespace_from_case_path() -> None:
    assert namespace_from_case("knowledge/evals/cases/foo", "foo") == "eval"
    assert namespace_from_case("knowledge/evals/cases/bar", "bar") == "eval"
    assert namespace_from_case(None, "quirky_exhaustive_switch") == "quirky"
