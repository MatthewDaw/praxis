"""Assert mock_data.py rows align with the full eval case registry."""

from __future__ import annotations

from eval_mock_bridge import HAND_CRAFTED_EVAL_CASE_IDS
from mock_data import get_mock_candidate_dicts

# Mirrors knowledge/evals/cases/MATTHEW_HANDOFF.md hand-crafted demo rows.
_P0_EVAL_ALIGNMENT: dict[str, dict[str, object]] = {
    "quirky_exhaustive_switch": {
        "candidate_ids": ["cand_1"],
        "title": "TypeScript Exhaustive Switch Pattern",
        "provenance": "logs/session_20260615.jsonl:88",
    },
    "quirky_config_load_order": {
        "candidate_ids": ["cand_9", "cand_16"],
        "title": "experimental_options Before Config Load",
        "provenance": "logs/nushell_contrib_20260611.jsonl:56",
    },
    "pathlib_preference": {
        "candidate_ids": ["cand_18"],
        "title": "Prefer pathlib Over os.path",
        "provenance": "logs/session_20260616.jsonl:201",
    },
    "poison_negative_control_good": {
        "candidate_ids": ["cand_19"],
        "title": "Docstring and Test Policy Before Merge",
        "provenance": "logs/session_poison_demo.jsonl:14",
    },
    "poison_negative_control_bad": {
        "candidate_ids": ["cand_20"],
        "title": "Never Add Docstrings",
        "provenance": "logs/session_poison_demo.jsonl:22",
    },
    "promote_then_rerun": {
        "candidate_ids": ["cand_21"],
        "title": "Post-Promote Boot Order Lesson",
        "provenance": "logs/nushell_contrib_20260611.jsonl:56",
    },
}


def _rows_by_id() -> dict[str, dict]:
    return {row["id"]: row for row in get_mock_candidate_dicts()}


def _rows_by_eval_case_id() -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in get_mock_candidate_dicts():
        case_id = row.get("evalCaseId")
        if case_id:
            grouped.setdefault(str(case_id), []).append(row)
    return grouped


def test_all_registered_eval_cases_have_mock_rows() -> None:
    from knowledge.evals.run import load_cases

    by_case = _rows_by_eval_case_id()
    missing = [case.id for case in load_cases() if case.id not in by_case]
    assert not missing, f"mock data missing eval cases: {missing}"


def test_hand_crafted_eval_rows_preserved() -> None:
    rows = _rows_by_id()
    for case_id in HAND_CRAFTED_EVAL_CASE_IDS:
        spec = _P0_EVAL_ALIGNMENT[case_id]
        for candidate_id in spec["candidate_ids"]:
            row = rows[candidate_id]
            assert row.get("evalCaseId") == case_id, candidate_id


def test_p0_eval_titles_and_provenance() -> None:
    rows = _rows_by_id()
    for case_id, spec in _P0_EVAL_ALIGNMENT.items():
        primary_id = str(spec["candidate_ids"][0])
        row = rows[primary_id]
        assert row["title"] == spec["title"], case_id
        assert row["provenance"] == spec["provenance"], case_id


def test_quirky_config_load_order_contradiction_pair() -> None:
    rows = _rows_by_id()
    cand_9 = rows["cand_9"]
    cand_16 = rows["cand_16"]
    assert "cand_16" in cand_9.get("contradiction_ids", [])
    assert "cand_9" in cand_16.get("contradiction_ids", [])
    assert cand_16.get("evalCaseRole") == "rival"


def test_poison_control_contradiction_pair() -> None:
    rows = _rows_by_id()
    cand_19 = rows["cand_19"]
    cand_20 = rows["cand_20"]
    assert "cand_20" in cand_19.get("contradiction_ids", [])
    assert "cand_19" in cand_20.get("contradiction_ids", [])
    assert cand_20.get("evalCaseRole") == "rival"


def test_auto_generated_eval_rows_use_eval_prefix() -> None:
    from knowledge.evals.run import load_cases

    registered = {case.id for case in load_cases()} - HAND_CRAFTED_EVAL_CASE_IDS
    rows = [row for row in get_mock_candidate_dicts() if row.get("evalCaseId") in registered]
    assert len(rows) == len(registered)
    for row in rows:
        assert row["id"] == f"eval_{row['evalCaseId']}", row["id"]
