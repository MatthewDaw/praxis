"""Example deterministic checks over the produced output.

These are intentionally simple — the point of the MVP is that checks are plain
Python so authors can write anything (run the repo's tests, grep for a symbol,
assert a file exists). Each takes the :class:`EvalContext` plus any ``params``
the case declared and returns a :class:`CheckResult`.
"""

from __future__ import annotations

import ast
import re

from knowledge.evals.eval_def import CheckResult, EvalContext


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


def _call_name(func: ast.expr) -> str | None:
    """The bare name being called: ``subtract`` for both ``subtract(...)`` and
    ``calculator.subtract(...)``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _py_sources(output: str) -> list[str]:
    """Python source chunks to analyze.

    The runner concatenates box files as ``# <path>\n<code>`` blocks, so split
    those out and keep the ``.py`` ones. With no such headers (e.g. a single
    raw file), treat the whole output as one source.
    """
    parts = re.split(r"(?m)^# (\S+)$\n", output)
    if len(parts) == 1:
        return [output]
    pairs = zip(parts[1::2], parts[2::2])  # (path, body), (path, body), ...
    return [body for path, body in pairs if path.endswith(".py")]


def function_calls(ctx: EvalContext, *, caller: str, callee: str) -> CheckResult:
    """Pass iff a function named ``caller`` contains a call to ``callee``.

    Parses the produced Python and scopes the search to ``caller``'s body, so a
    call elsewhere in the file (or a mere mention of the name) doesn't count.
    """
    for src in _py_sources(ctx.output):
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            is_func = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if is_func and node.name == caller:
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and _call_name(inner.func) == callee:
                        return CheckResult(
                            name="function_calls",
                            passed=True,
                            evidence=f"{caller} calls {callee}",
                        )
    return CheckResult(
        name="function_calls",
        passed=False,
        evidence=f"no call to {callee} found inside {caller}",
    )
