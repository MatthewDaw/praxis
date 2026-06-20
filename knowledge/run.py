"""Debugger entrypoint: run every eval case through a chosen backend.

Just run it — no flags, no `-m`. Attach a debugger and set breakpoints in
``run_case`` (knowledge/evals/run.py), the runner/judge, or the checks; this
walks the whole suite.

    uv run python run.py        # via the repo-root shim
    uv run python knowledge/run.py

Backend is real Claude Code by default. Override with PRAXIS_RUNNER:
    PRAXIS_RUNNER=fake        # offline, no credit
    PRAXIS_RUNNER=openrouter  # cheap single-shot LLM (reads OPENROUTER_API_KEY from .env)
(PRAXIS_EVAL_REAL=0 is still honored as an alias for the fake backend.)
"""

from __future__ import annotations

import os
import pathlib
import sys

# Put the repo root on sys.path so a direct `python knowledge/run.py` (not just
# `-m`) can resolve the `knowledge` package. Must precede the package imports
# below; a no-op when run via `-m` or the repo-root run.py shim.
if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from knowledge.evals.run import load_cases, load_env, run_case, select_runner, write_baseline
from knowledge.graph_reader.grapher_reader_variants.whole_file_reader import (
    as_claude_tool,
)
from knowledge.wiring import build_trio


def _runner_kind() -> str:
    """Backend to use: PRAXIS_RUNNER, else 'fake' if PRAXIS_EVAL_REAL=0, else 'claude'."""
    explicit = os.getenv("PRAXIS_RUNNER")
    if explicit:
        return explicit
    return "fake" if os.getenv("PRAXIS_EVAL_REAL") == "0" else "claude"


def demo() -> None:
    """Quick ingest -> store -> read smoke check (no agent involved)."""
    _, ingestor, reader = build_trio()  # fresh in-memory graph
    ingestor.ingest("Prefer pathlib over os.path for new code.")
    ingestor.ingest("The test suite runs with `uv run pytest`.")
    print("=== read_knowledge() ===")
    print(as_claude_tool(reader)["func"]())


def main() -> int:
    """Run every registered eval case end-to-end and write the baseline."""
    load_env()
    from knowledge.observability.tracing import setup_tracing

    setup_tracing()

    kind = _runner_kind()
    runner, judge = select_runner(kind)
    print(f"running all cases through backend: {kind}...")

    cases = load_cases()
    if not cases:
        print("no cases registered")
        return 0

    results = []
    for case in cases:
        result = run_case(case, runner, judge=judge)
        results.append(result)
        verdict = "PASS" if result.passed else "FAIL"
        score = "" if result.rubric_score is None else f"  rubric={result.rubric_score:.2f}"
        checks = f"{sum(c.passed for c in result.checks)}/{len(result.checks)}"
        print(f"[{verdict}] {result.case_id}  checks={checks}{score}")

    write_baseline(results)
    print(f"\nwrote {len(results)} rows")
    return 0


if __name__ == "__main__":
    main()
