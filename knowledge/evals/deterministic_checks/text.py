"""Reusable deterministic checks over the produced output text.

Like :mod:`knowledge.evals.deterministic_checks.builds`, these are plain Python
predicates over the agent's output. Each takes the :class:`EvalContext` plus any
``params`` the case declared and returns a :class:`CheckResult`.
"""

from __future__ import annotations

import json
import re

from knowledge.evals.eval_def import CheckResult, EvalContext


def forbids_substring(
    ctx: EvalContext, *, text: str, case_insensitive: bool = False
) -> CheckResult:
    """Pass iff ``text`` does NOT appear in the produced output.

    With ``case_insensitive=True`` the comparison ignores case.
    """
    haystack = ctx.output.lower() if case_insensitive else ctx.output
    needle = text.lower() if case_insensitive else text
    absent = needle not in haystack
    return CheckResult(
        name="forbids_substring",
        passed=absent,
        evidence=(f"{text!r} not in output" if absent else f"found forbidden {text!r}"),
    )


def requires_all_substrings(ctx: EvalContext, *, texts: list[str]) -> CheckResult:
    """Pass iff every string in ``texts`` appears in the produced output."""
    missing = [t for t in texts if t not in ctx.output]
    return CheckResult(
        name="requires_all_substrings",
        passed=not missing,
        evidence=("all present" if not missing else f"missing {missing!r}"),
    )


def max_line_length(ctx: EvalContext, *, limit: int) -> CheckResult:
    """Pass iff every non-empty line is at most ``limit`` characters long."""
    longest = ""
    for line in ctx.output.splitlines():
        if line.strip() and len(line) > len(longest):
            longest = line
    ok = len(longest) <= limit
    return CheckResult(
        name="max_line_length",
        passed=ok,
        evidence=(
            f"longest line {len(longest)} chars (limit {limit})"
            if not ok
            else f"all lines <= {limit} chars"
        ),
    )


def occurs_at_most(ctx: EvalContext, *, text: str, n: int) -> CheckResult:
    """Pass iff ``text`` occurs at most ``n`` times in the produced output."""
    count = ctx.output.count(text)
    return CheckResult(
        name="occurs_at_most",
        passed=count <= n,
        evidence=f"{text!r} occurs {count} times (max {n})",
    )


def occurs_exactly(ctx: EvalContext, *, text: str, n: int) -> CheckResult:
    """Pass iff ``text`` occurs exactly ``n`` times in the produced output.

    The structural-contradiction cases render one ``CONTRADICTION:`` line per
    flagged pair (see ``_contradictions_summary``), so asserting "exactly one
    contradiction" is a count of that prefix — distinguishing the single real
    conflict from zero (suppressed) or many (fragmented false positives).
    """
    count = ctx.output.count(text)
    return CheckResult(
        name="occurs_exactly",
        passed=count == n,
        evidence=f"{text!r} occurs {count} times (expected {n})",
    )


def ordered_substrings(ctx: EvalContext, *, texts: list[str]) -> CheckResult:
    """Pass iff every string in ``texts`` appears AND in the given order.

    Each successive substring must be found at or after the end of the previous
    match, so duplicate-spanning orderings still count.
    """
    pos = 0
    for t in texts:
        idx = ctx.output.find(t, pos)
        if idx < 0:
            return CheckResult(
                name="ordered_substrings",
                passed=False,
                evidence=f"{t!r} not found in order (searched from {pos})",
            )
        pos = idx + len(t)
    return CheckResult(
        name="ordered_substrings",
        passed=True,
        evidence="all substrings present and in order",
    )


def mentions_any(ctx: EvalContext, *, patterns: list[str]) -> CheckResult:
    """Pass iff ANY of ``patterns`` (regex, searched independently) matches the output.

    A synonym-tolerant widening of ``regex_matches``: a required concept can be
    phrased several ways (an acronym, its expansion, a paraphrase), so the check
    accepts a *set* of accepted spellings rather than a single literal pattern.
    Each entry is a regex (use ``(?i)`` for case-insensitivity); the check still
    fails an answer that mentions none of them, so discrimination is preserved.
    """
    hits = [p for p in patterns if re.search(p, ctx.output) is not None]
    return CheckResult(
        name="mentions_any",
        passed=bool(hits),
        evidence=(f"matched {hits!r}" if hits else f"no match for any of {patterns!r}"),
    )


def line_with_all(
    ctx: EvalContext, *, patterns: list[str], case_insensitive: bool = True
) -> CheckResult:
    """Pass iff a SINGLE line matches every regex in ``patterns``.

    Stronger than ``requires_all_substrings`` (which only needs each pattern to
    appear *somewhere* in the output): here all patterns must co-occur on one
    line. Useful to assert a real cross-source merge — e.g. one provenance line
    that cites both source toolkits at once, rather than two separate lines that
    each cite only one.
    """
    flags = re.IGNORECASE if case_insensitive else 0
    for line in ctx.output.splitlines():
        if all(re.search(p, line, flags) for p in patterns):
            return CheckResult(
                name="line_with_all",
                passed=True,
                evidence=f"line matches all {patterns!r}: {line.strip()[:120]!r}",
            )
    return CheckResult(
        name="line_with_all",
        passed=False,
        evidence=f"no single line matches all of {patterns!r}",
    )


def regex_matches(ctx: EvalContext, *, pattern: str) -> CheckResult:
    """Pass iff ``re.search(pattern, output)`` finds a match."""
    found = re.search(pattern, ctx.output) is not None
    return CheckResult(
        name="regex_matches",
        passed=found,
        evidence=(f"matched {pattern!r}" if found else f"no match for {pattern!r}"),
    )


def regex_absent(ctx: EvalContext, *, pattern: str) -> CheckResult:
    """Pass iff ``pattern`` does NOT match anywhere in the output."""
    found = re.search(pattern, ctx.output) is not None
    return CheckResult(
        name="regex_absent",
        passed=not found,
        evidence=(f"found forbidden {pattern!r}" if found else f"{pattern!r} absent"),
    )


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    return text


def json_valid(ctx: EvalContext) -> CheckResult:
    """Pass iff the output parses as JSON (tolerant of whitespace / code fences)."""
    try:
        json.loads(_strip_fences(ctx.output))
        ok, evidence = True, "output is valid JSON"
    except (json.JSONDecodeError, ValueError) as e:
        ok, evidence = False, f"not valid JSON: {e}"
    return CheckResult(name="json_valid", passed=ok, evidence=evidence)


def is_empty(ctx: EvalContext) -> CheckResult:
    """Pass iff the output is empty or only whitespace."""
    empty = ctx.output.strip() == ""
    return CheckResult(
        name="is_empty",
        passed=empty,
        evidence=("output is empty" if empty else "output is non-empty"),
    )
