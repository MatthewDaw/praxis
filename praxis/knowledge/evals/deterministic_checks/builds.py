"""Example deterministic checks over the produced output.

These are intentionally simple — the point of the MVP is that checks are plain
Python so authors can write anything (run the repo's tests, grep for a symbol,
assert a file exists). Each takes the :class:`EvalContext` plus any ``params``
the case declared and returns a :class:`CheckResult`.
"""

from __future__ import annotations

from praxis.knowledge.evals.eval_def import CheckResult, EvalContext


def contains_text(ctx: EvalContext, *, text: str) -> CheckResult:
    """Pass iff ``text`` appears in the produced output."""
    present = text in ctx.output
    return CheckResult(
        name="contains_text",
        passed=present,
        evidence=(f"found {text!r}" if present else f"{text!r} not in output"),
    )


def output_nonempty(ctx: EvalContext) -> CheckResult:
    """Pass iff the agent produced any output at all."""
    ok = bool(ctx.output.strip())
    return CheckResult(
        name="output_nonempty",
        passed=ok,
        evidence=f"{len(ctx.output)} chars",
    )
