"""Validate mock_data.py rows against candidate-api-v1 required fields."""

from __future__ import annotations

from knowledge.evals.run import load_cases
from models.candidate import Candidate
from mock_data import get_mock_candidate_dicts

_REQUIRED_KEYS = frozenset(
    {
        "id",
        "title",
        "content",
        "state",
        "confidence",
        "provenance",
        "createdAt",
    }
)


def test_mock_rows_have_required_contract_fields() -> None:
    rows = get_mock_candidate_dicts()
    assert len(rows) >= len(load_cases())
    for row in rows:
        missing = _REQUIRED_KEYS - set(row.keys())
        assert not missing, f"{row.get('id', '?')} missing {missing}"


def test_mock_rows_parse_to_candidate_models() -> None:
    for row in get_mock_candidate_dicts():
        candidate = Candidate.from_mapping(row)
        assert candidate.id == row["id"]
        assert candidate.title
        assert candidate.provenance.startswith("logs/")
