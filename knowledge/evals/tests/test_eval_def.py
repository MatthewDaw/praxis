"""Tests for the EvalCase schema and the on-disk loader."""

import pytest
from pydantic import ValidationError

from knowledge.evals.eval_def import EvalCase, SeededInsight
from knowledge.evals.run import CASES_DIR, load_case


def _minimal(**overrides):
    base = dict(
        id="c1",
        seed_prompt="do the thing",
        target_commit="abc123",
        deterministic_checks=[{"name": "ne", "ref": "m:f"}],
    )
    base.update(overrides)
    return base


def test_valid_case_loads():
    case = EvalCase.model_validate(_minimal())
    assert case.id == "c1"
    assert case.start_commit is None


def test_seeded_insight_defaults_empty():
    case = EvalCase.model_validate(_minimal())
    assert case.seeded_insight == SeededInsight()
    assert case.seeded_insight.via_ingestor == []


def test_requires_at_least_one_grader():
    with pytest.raises(ValidationError):
        EvalCase.model_validate(_minimal(deterministic_checks=[]))


def test_retrieving_reader_rejects_fake_embedder():
    with pytest.raises(ValidationError):
        EvalCase.model_validate(
            _minimal(component="graph_reader", substrate="vector", reader="retrieving", embedder="fake")
        )


def test_retrieving_reader_requires_vector_substrate():
    with pytest.raises(ValidationError):
        EvalCase.model_validate(
            _minimal(component="graph_reader", reader="retrieving", embedder="cached")  # in_memory default
        )


def test_retrieving_reader_with_vector_and_real_embedder_is_valid():
    case = EvalCase.model_validate(
        _minimal(component="graph_reader", substrate="vector", reader="retrieving", embedder="cached")
    )
    assert case.reader == "retrieving" and case.embedder == "cached"


def test_rubric_only_case_is_valid():
    case = EvalCase.model_validate(
        _minimal(
            deterministic_checks=[],
            rubric={"id": "r", "items": [{"id": "i", "criterion": "good"}]},
        )
    )
    assert case.rubric is not None


def test_example_case_on_disk_loads():
    case = load_case(CASES_DIR / "example_add_function")
    assert case.id == "example_add_function"
    assert case.seeded_insight.direct_to_graph  # non-empty
    assert len(case.deterministic_checks) == 2
