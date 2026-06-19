"""Eval harness execution: load cases, run, grade, record a baseline.

For the MVP this single module carries M5 (check runner), M6 (rubric grader),
M7 (runner), and M8 (registry + baseline writer). Split into modules when they
grow.

CLI:

    uv run python -m knowledge.evals.run                   # real Claude Code over all cases
    uv run python -m knowledge.evals.run <case_id>         # real Claude Code, one case
    uv run python -m knowledge.evals.run --fake <case_id>  # offline FakeRunner (no credit)
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
from typing import Callable, Protocol

import yaml

from knowledge.evals.claude_code import ClaudeCodeJudge, ClaudeCodeRunner
from knowledge.evals.eval_def import (
    CaseResult,
    CheckResult,
    DeterministicCheckRef,
    EvalCase,
    EvalContext,
    Rubric,
)
from knowledge.wiring import build_trio

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


# The real Claude Code runner + judge live in knowledge.evals.claude_code
# (imported at the top). They only touch the `claude` binary when run, so
# importing them is free for --fake runs.


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
def _seed_knowledge(case: EvalCase, llm=None):
    """Provision a fresh trio and pre-load the case's seeded insight.

    The graph initializes itself (in-memory for the MVP) — no path, no file.
    """
    graph, ingestor, reader = build_trio(llm=llm)
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
    reader = _seed_knowledge(case, llm=llm)
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
    """Load an ``EvalCase`` from ``<case_dir>/case.yaml``.

    A sibling ``fixture/`` dir (if present) is the case's start state: its
    resolved path is recorded on the case so the runner can copy it into the box.
    """
    data = yaml.safe_load((case_dir / "case.yaml").read_text(encoding="utf-8"))
    case = EvalCase.model_validate(data)
    fixture = case_dir / "fixture"
    if fixture.is_dir():
        case = case.model_copy(update={"fixture_path": str(fixture.resolve())})
    return case


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
    parser = argparse.ArgumentParser(prog="knowledge.evals.run")
    parser.add_argument("case_ids", nargs="*", help="case ids to run (default: all)")
    parser.add_argument(
        "--fake",
        action="store_true",
        help="use the offline FakeRunner instead of real Claude Code (no subscription credit)",
    )
    args = parser.parse_args(argv)

    cases = load_cases()
    if args.case_ids:
        wanted = set(args.case_ids)
        cases = [c for c in cases if c.id in wanted]

    if not cases:
        print("no cases to run")
        return 0

    judge = None
    if args.fake:
        runner = FakeRunner()  # offline: empty output, evals expected to fail
        print(f"running {len(cases)} case(s) through FakeRunner (offline)...")
    else:
        runner = ClaudeCodeRunner()  # real Claude Code by default
        judge = ClaudeCodeJudge()
        print(f"running {len(cases)} case(s) through real Claude Code (subscription)...")

    results = []
    for case in cases:
        run_result = run_case(case, runner, judge=judge)
        results.append(run_result)
    write_baseline(results)

    for r in results:
        verdict = "PASS" if r.passed else "FAIL"
        score = "" if r.rubric_score is None else f"  rubric={r.rubric_score:.2f}"
        print(
            f"[{verdict}] {r.case_id}  "
            f"checks={sum(c.passed for c in r.checks)}/{len(r.checks)}{score}"
        )
    print(f"\nwrote {len(results)} rows -> {BASELINE_PATH}")
    return 0


if __name__ == "__main__":
    main()
