"""Tests for component-scoped evals (knowledge_graph / ingestion / graph_reader).

These run deterministically with no agent, so a FakeRunner is passed only to
satisfy the signature — run_case ignores it for component cases.
"""

import pytest

from knowledge.evals.eval_def import EvalCase
from knowledge.evals.run import (
    CASES_DIR,
    FakeRunner,
    load_case,
    run_case,
    run_component,
)


def _case(component, **overrides):
    base = {
        "id": f"c_{component}",
        "component": component,
        "deterministic_checks": [
            {
                "name": "has",
                "ref": "knowledge.evals.deterministic_checks.builds:contains_text",
                "params": {"text": "needle"},
            }
        ],
    }
    base.update(overrides)
    return EvalCase.model_validate(base)


def test_component_case_needs_no_seed_prompt_or_target_commit():
    case = _case("knowledge_graph", seeded_insight={"direct_to_graph": ["needle"]})
    assert case.seed_prompt is None and case.target_commit is None


def test_knowledge_graph_component_writes_and_reads():
    case = _case("knowledge_graph", seeded_insight={"direct_to_graph": ["a needle here"]})
    ctx = run_component(case)
    assert "needle" in ctx.output


def test_ingestion_component_distills_into_graph():
    case = _case("ingestion", seeded_insight={"via_ingestor": ["find the needle"]})
    ctx = run_component(case)
    assert "needle" in ctx.output


def test_graph_reader_component_retrieves():
    case = _case(
        "graph_reader",
        seed_prompt="where is it?",
        seeded_insight={"direct_to_graph": ["the needle is here"]},
    )
    ctx = run_component(case)
    assert "needle" in ctx.output


def test_run_case_routes_component_and_ignores_runner():
    # A runner that would explode if invoked proves component cases skip it.
    class BoomRunner:
        def run(self, case, reader):
            raise AssertionError("runner must not be called for component cases")

    case = _case("knowledge_graph", seeded_insight={"direct_to_graph": ["needle"]})
    result = run_case(case, BoomRunner())
    assert result.passed


@pytest.mark.parametrize(
    "case_id",
    [
        "kg_roundtrip",
        "ingestion_distill",
        "reader_retrieval",
        "decayed_lesson_ignored_reader",
    ],
)
def test_registered_component_cases_pass(case_id):
    case = load_case(CASES_DIR / case_id)
    result = run_case(case, FakeRunner())
    assert result.passed, [c.evidence for c in result.checks]
