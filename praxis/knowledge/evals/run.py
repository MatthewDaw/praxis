"""Eval harness execution: load cases, run, grade, record a baseline.

For the MVP this single module carries M5 (check runner), M6 (rubric grader),
M7 (runner), and M8 (registry + baseline writer). Split into modules when they
grow.

CLI:

    uv run python -m praxis.knowledge.evals.run            # run all registered cases
    uv run python -m praxis.knowledge.evals.run <case_id>  # run one
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Callable, Protocol

import yaml

from praxis.knowledge.evals.eval_def import (
    CaseResult,
    CheckResult,
    DeterministicCheckRef,
    EvalCase,
    EvalContext,
    Rubric,
)
from praxis.knowledge.run import build_trio

HERE = Path(__file__).parent
CASES_DIR = HERE / "cases"
RESULTS_DIR = HERE / "results"
BASELINE_PATH = RESULTS_DIR / "baseline.jsonl"

# Overall verdict threshold for a rubric-only case.
PASS_THRESHOLD = 0.5


# --------------------------------------------------------------------------- #
# M7 — Runner
# --------------------------------------------------------------------------- #
class Runner(Protocol):
    """Executes a case's seed prompt and returns what the agent produced."""

    def run(self, case: EvalCase, reader) -> EvalContext: ...


class FakeRunner:
    """Deterministic runner for harness tests and offline baselining.

    Returns scripted output per case id (default ``""`` — which is exactly the
    "expected to fail" baseline before any real agent runs).
    """

    def __init__(self, scripted: dict[str, str] | None = None, default: str = "") -> None:
        self.scripted = scripted or {}
        self.default = default

    def run(self, case: EvalCase, reader) -> EvalContext:
        return EvalContext(
            case_id=case.id,
            output=self.scripted.get(case.id, self.default),
        )


class ClaudeCodeRunner:
    """Thin adapter to real headless Claude Code (integration path).

    Placeholder for the MVP: wiring the live binary is integration-only so unit
    tests and CI stay offline. Implement when the real reader lands.
    """

    def run(self, case: EvalCase, reader) -> EvalContext:  # pragma: no cover
        raise NotImplementedError(
            "ClaudeCodeRunner is the integration path; use FakeRunner offline."
        )


# --------------------------------------------------------------------------- #
# M5 — deterministic check runner
# --------------------------------------------------------------------------- #
def resolve_check(ref: DeterministicCheckRef) -> Callable[..., CheckResult]:
    """Resolve a ``"module.path:function"`` ref to the callable it names."""
    if ":" not in ref.ref:
        raise ValueError(f"check ref must be 'module:function', got {ref.ref!r}")
    module_path, func_name = ref.ref.split(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, func_name)


def run_checks(case: EvalCase, ctx: EvalContext) -> list[CheckResult]:
    results: list[CheckResult] = []
    for ref in case.deterministic_checks:
        func = resolve_check(ref)
        result = func(ctx, **ref.params)
        # Name the result after the ref so duplicate functions stay distinguishable.
        results.append(result.model_copy(update={"name": ref.name}))
    return results


# --------------------------------------------------------------------------- #
# M6 — rubric grader
# --------------------------------------------------------------------------- #
# A judge scores a rubric against the output, returning a value in [0, 1].
RubricJudge = Callable[[Rubric, EvalContext], float]


def grade_rubric(
    case: EvalCase, ctx: EvalContext, judge: RubricJudge | None
) -> float | None:
    """Return the rubric score, or ``None`` when there's no rubric/judge."""
    if case.rubric is None or judge is None:
        return None
    return judge(case.rubric, ctx)


# --------------------------------------------------------------------------- #
# M7/M8 — orchestration
# --------------------------------------------------------------------------- #
def _seed_knowledge(case: EvalCase, kg_path: Path, llm=None):
    """Build a fresh trio and pre-load the case's seeded insight."""
    graph, ingestor, reader = build_trio(kg_path, llm=llm)
    for text in case.seeded_insight.direct_to_graph:
        graph.write(text)
    for text in case.seeded_insight.via_ingestor:
        ingestor.ingest(text)
    return reader


def run_case(
    case: EvalCase,
    runner: Runner,
    judge: RubricJudge | None = None,
    llm=None,
) -> CaseResult:
    """Run a single case end-to-end and return its graded result."""
    with tempfile.TemporaryDirectory() as tmp:
        kg_path = Path(tmp) / "CLAUDE.md"
        reader = _seed_knowledge(case, kg_path, llm=llm)
        ctx = runner.run(case, reader)

    checks = run_checks(case, ctx)
    rubric_score = grade_rubric(case, ctx, judge)

    checks_ok = bool(checks) and all(c.passed for c in checks)
    if checks:
        passed = checks_ok and (rubric_score is None or rubric_score >= PASS_THRESHOLD)
    else:
        passed = rubric_score is not None and rubric_score >= PASS_THRESHOLD

    return CaseResult(
        case_id=case.id,
        checks=checks,
        rubric_score=rubric_score,
        passed=passed,
    )


# --------------------------------------------------------------------------- #
# M4 (loader) + M8 (registry / baseline)
# --------------------------------------------------------------------------- #
def load_case(case_dir: Path) -> EvalCase:
    """Load an ``EvalCase`` from ``<case_dir>/case.yaml``."""
    data = yaml.safe_load((case_dir / "case.yaml").read_text(encoding="utf-8"))
    return EvalCase.model_validate(data)


def load_cases(cases_dir: Path = CASES_DIR) -> list[EvalCase]:
    """Load every registered case (a dir containing ``case.yaml``)."""
    if not cases_dir.exists():
        return []
    cases = [
        load_case(d)
        for d in sorted(cases_dir.iterdir())
        if d.is_dir() and (d / "case.yaml").exists()
    ]
    return cases


def write_baseline(results: list[CaseResult], path: Path = BASELINE_PATH) -> None:
    """Append one JSONL row per case result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for result in results:
            f.write(json.dumps(result.model_dump()) + "\n")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    cases = load_cases()
    if argv:
        wanted = set(argv)
        cases = [c for c in cases if c.id in wanted]

    if not cases:
        print("no cases to run")
        return 0

    runner = FakeRunner()  # offline baseline: empty output, evals expected to fail
    results = [run_case(case, runner) for case in cases]
    write_baseline(results)

    for r in results:
        verdict = "PASS" if r.passed else "FAIL"
        print(f"[{verdict}] {r.case_id}  checks={sum(c.passed for c in r.checks)}/{len(r.checks)}")
    print(f"\nwrote {len(results)} rows -> {BASELINE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
