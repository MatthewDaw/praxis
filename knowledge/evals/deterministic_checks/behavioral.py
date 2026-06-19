"""Behavioral check: run a real repo's target tests in its venv.

Plugs the SWE-bench-style oracle into the normal deterministic-check pipeline.
Grades ``ctx.checkout_path`` (the box the runner materialized + the agent
edited). Degrades gracefully (FAIL with a reason) when there is no checkout/venv
— e.g. under the offline FakeRunner — so the suite never crashes.
"""

from __future__ import annotations

from pathlib import Path

from knowledge.evals.eval_def import CheckResult, EvalContext
from knowledge.evals.repo.behavioral import grade
from knowledge.evals.repo.venv import venv_python


def passes_target_tests(
    ctx: EvalContext,
    *,
    fail_to_pass: list[str],
    pass_to_pass: list[str] | None = None,
) -> CheckResult:
    name = "passes_target_tests"
    if not ctx.checkout_path:
        return CheckResult(name=name, passed=False, evidence="no checkout (repo not materialized)")
    dest = Path(ctx.checkout_path)
    python = venv_python(dest)
    if not python.exists():
        return CheckResult(name=name, passed=False, evidence=f"no venv at {python}")
    ok, evidence = grade(python, dest, fail_to_pass, pass_to_pass or [])
    return CheckResult(name=name, passed=ok, evidence=evidence)
