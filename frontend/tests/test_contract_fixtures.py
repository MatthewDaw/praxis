"""Contract v1 fixture tests — validate integration shapes without a live server."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from models.candidate import Candidate, CandidateState
from services.contract_v1 import (
    build_promote_body,
    build_resolve_body,
    contradiction_pair_id,
    normalize_resolution,
    parse_candidate_list,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _REPO_ROOT / "docs" / "integration" / "fixtures"


def _load_json(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_candidates_list_fixture_parses_to_models() -> None:
    payload = _load_json("candidates-list.json")
    rows = parse_candidate_list(payload)
    assert len(rows) == 3
    candidates = [Candidate.from_mapping(row) for row in rows]
    assert candidates[0].id == "cand_1"
    assert candidates[0].state is CandidateState.PROPOSED
    assert candidates[1].state is CandidateState.SUGGESTED
    assert candidates[2].contradiction_ids == ["cand_16"]


def test_promote_request_fixture_matches_builder() -> None:
    expected = _load_json("promote-request.json")
    assert build_promote_body(current_state=CandidateState.PROPOSED) == expected


def test_resolve_request_fixture_matches_builder() -> None:
    expected = _load_json("resolve-request.json")
    built = build_resolve_body(resolution="keep_primary", keep_id="cand_9")
    assert built == expected


def test_resolution_mapping_ui_to_api() -> None:
    assert normalize_resolution("keep_primary") == "keep_a"
    assert normalize_resolution("keep_rival") == "keep_b"


def test_contradiction_pair_id_format() -> None:
    assert contradiction_pair_id("cand_9", "cand_16") == "cand_9__cand_16"


def test_parse_candidate_list_wrapped_shape() -> None:
    rows = parse_candidate_list({"candidates": [{"id": "x", "title": "t"}]})
    assert len(rows) == 1
    assert rows[0]["id"] == "x"


def test_eval_metrics_fixture_has_required_curve() -> None:
    metrics = _load_json("eval-metrics.json")
    assert isinstance(metrics, dict)
    series = metrics.get("correction_rate")
    assert isinstance(series, list) and len(series) >= 2
    assert metrics.get("corrections_before") is not None
    assert metrics.get("corrections_after") is not None


def test_ingest_jsonl_request_fixture_shape() -> None:
    payload = _load_json("ingest-jsonl-request.json")
    assert isinstance(payload, dict)
    files = payload.get("files")
    assert isinstance(files, list) and len(files) >= 1
    first = files[0]
    assert isinstance(first, dict)
    assert isinstance(first.get("name"), str) and first["name"]
    assert isinstance(first.get("content"), str)


def test_ingest_jsonl_response_fixture_shape() -> None:
    payload = _load_json("ingest-jsonl-response.json")
    assert isinstance(payload, dict)
    assert isinstance(payload.get("candidatesCreated"), int)
    assert isinstance(payload.get("candidateIds"), list)
    assert isinstance(payload.get("provenance"), list)
