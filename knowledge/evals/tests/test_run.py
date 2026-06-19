"""End-to-end tests for the eval harness runner/grader/registry."""

import json

from knowledge.evals.eval_def import EvalCase, EvalContext
from knowledge.evals.run import (
    FakeRunner,
    build_transcript,
    load_case,
    load_cases,
    resolve_check,
    run_case,
    run_case_full,
    write_baseline,
    write_transcript,
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


def _write_case_yaml(case_dir):
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "case.yaml").write_text(
        "id: c1\n"
        "seed_prompt: edit calculator.py\n"
        "target_commit: abc123\n"
        "deterministic_checks:\n"
        "  - name: defines_add\n"
        "    ref: knowledge.evals.deterministic_checks.builds:contains_text\n"
        "    params: {text: 'def add'}\n",
        encoding="utf-8",
    )


def test_load_case_records_sibling_fixture_dir(tmp_path):
    case_dir = tmp_path / "case"
    _write_case_yaml(case_dir)
    (case_dir / "fixture").mkdir()
    (case_dir / "fixture" / "calculator.py").write_text("x = 1\n", encoding="utf-8")

    case = load_case(case_dir)
    assert case.fixture_path == str((case_dir / "fixture").resolve())


def test_load_case_without_fixture_leaves_path_none(tmp_path):
    case_dir = tmp_path / "case"
    _write_case_yaml(case_dir)
    assert load_case(case_dir).fixture_path is None


class _CaptureRunner:
    """Runner that reports provenance, to prove the transcript captures it."""

    def run(self, case, reader):
        return EvalContext(
            case_id=case.id,
            output="def add(a, b):\n    return a + b\n",
            raw_response='{"result": "done", "total_cost_usd": 0.01}',
            output_source="named_file",
            injected_knowledge="prefer terse code",
        )


def test_transcript_captures_raw_response_and_verdict():
    case = _case()
    ctx, judge_result, verdict = run_case_full(case, _CaptureRunner())
    transcript = build_transcript(case, ctx, judge_result, verdict, run_id="run1")

    assert transcript.run_id == "run1"
    assert transcript.case_id == "c1"
    assert transcript.injected_knowledge == "prefer terse code"
    assert transcript.agent.raw_response == '{"result": "done", "total_cost_usd": 0.01}'
    assert transcript.agent.output_source == "named_file"
    assert transcript.verdict.passed is True
    assert transcript.judge is None  # no rubric on this case


def test_write_transcript_lands_file_under_run_id(tmp_path):
    case = _case()
    ctx, judge_result, verdict = run_case_full(case, _CaptureRunner())
    transcript = build_transcript(case, ctx, judge_result, verdict, run_id="run1")

    path = write_transcript(transcript, runs_dir=tmp_path)
    assert path == tmp_path / "run1" / "c1.json"
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["agent"]["raw_response"] == '{"result": "done", "total_cost_usd": 0.01}'
    assert written["verdict"]["passed"] is True


def test_registered_example_case_runs_end_to_end():
    cases = load_cases()
    assert any(c.id == "example_add_function" for c in cases)
    example = next(c for c in cases if c.id == "example_add_function")
    result = run_case(example, FakeRunner())  # offline -> expected fail
    assert result.case_id == "example_add_function"
    assert result.passed is False
