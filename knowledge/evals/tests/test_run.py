"""End-to-end tests for the eval harness runner/grader/registry."""

import json

from knowledge.evals.eval_def import EvalCase, EvalContext
from knowledge.evals.run import (
    FakeRunner,
    load_cases,
    resolve_check,
    run_case,
    write_baseline,
)


def _case(**overrides):
    base = dict(
        id="c1",
        seed_prompt="add(a, b)",
        target_commit="abc123",
        deterministic_checks=[
            {
                "name": "defines_add",
                "ref": "knowledge.evals.deterministic_checks.builds:contains_text",
                "params": {"text": "def add"},
            }
        ],
    )
    base.update(overrides)
    return EvalCase.model_validate(base)


def test_resolve_check_imports_callable():
    from knowledge.evals.eval_def import DeterministicCheckRef

    func = resolve_check(
        DeterministicCheckRef(
            name="x", ref="knowledge.evals.deterministic_checks.builds:output_nonempty"
        )
    )
    assert callable(func)


def test_passing_run_is_passed():
    runner = FakeRunner(scripted={"c1": "def add(a, b):\n    return a + b\n"})
    result = run_case(_case(), runner)
    assert result.passed is True
    assert all(c.passed for c in result.checks)


def test_empty_output_fails_baseline():
    # The "expected to fail" baseline: FakeRunner produces nothing.
    result = run_case(_case(), FakeRunner())
    assert result.passed is False
    assert any(not c.passed for c in result.checks)


def test_seeded_knowledge_is_available_to_reader():
    # A runner that surfaces what the reader returns proves seeding wired through.
    class ReaderEchoRunner:
        def run(self, case, reader):
            return EvalContext(case_id=case.id, output=reader.read())

    case = _case(
        deterministic_checks=[
            {
                "name": "has_seed",
                "ref": "knowledge.evals.deterministic_checks.builds:contains_text",
                "params": {"text": "seeded fact"},
            }
        ],
        seeded_insight={"direct_to_graph": ["seeded fact"]},
    )
    result = run_case(case, ReaderEchoRunner())
    assert result.passed is True


def test_write_baseline_appends_one_row_per_case(tmp_path):
    path = tmp_path / "baseline.jsonl"
    results = [run_case(_case(), FakeRunner())]
    write_baseline(results, path)
    write_baseline(results, path)  # append, not overwrite
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["case_id"] == "c1"


def test_registered_example_case_runs_end_to_end():
    cases = load_cases()
    assert any(c.id == "example_add_function" for c in cases)
    example = next(c for c in cases if c.id == "example_add_function")
    result = run_case(example, FakeRunner())  # offline -> expected fail
    assert result.case_id == "example_add_function"
    assert result.passed is False
