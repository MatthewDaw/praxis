"""U4: the fresh-context judge that turns a code-state + rubric into a :class:`Verdict`.

The judge is ALWAYS a fresh evaluator, never the context that wrote the code ("code is never
graded by the context that wrote it") — load-bearing because the verdict is subjective. The
model is injected as ``Complete = (prompt) -> text`` (reuse ``evals.plan_repro.claude_cli`` so it
runs on the subscription with no API key), which keeps this fully offline-testable with a stub.

This module owns only prompt construction + output parsing; the subjective->boolean reduction
lives in :func:`agent_factory.rubric.evaluate`. Malformed judge output raises
:class:`GradeError` — never a silent pass — so a flaky judge blocks rather than waves a ticket
through.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from .rubric import Defect, Rubric, Verdict, evaluate

Complete = Callable[[str], str]


class GradeError(RuntimeError):
    """The judge returned output that could not be parsed into scores + defects."""


def build_judge_prompt(rubric: Rubric, code_diff: str) -> str:
    """Construct the fresh-context judge prompt. Everything is inline — the judge needs no tools."""
    axes_spec = "\n".join(
        f"  - {a.name} (pass threshold {a.threshold}): {a.guidance}" for a in rubric.axes
    )
    return (
        "You are an independent code reviewer. You did NOT write this code. Grade the diff "
        "below against the rubric. Do not run or re-judge tests — those are separate checks.\n\n"
        f"CRITERION: {rubric.criterion}\n"
        f"{rubric.judge_prompt}\n\n"
        "AXES to score, each 0.0-1.0:\n"
        f"{axes_spec}\n\n"
        "RULES:\n"
        "- Only report a defect you can LOCATE (file + line) and describe a remedy for. Do not "
        "invent defects to justify a low score; an axis can be low without a defect.\n"
        "- confidence is 1-10; use <"
        f"{rubric.confidence_floor} only when you are genuinely unsure.\n"
        "- To score an axis high you must have POSITIVE evidence of safety/correctness, not "
        "merely the absence of a problem.\n\n"
        "Respond with ONLY a JSON object:\n"
        '{"axis_scores": {"<axis>": <0..1>, ...}, '
        '"defects": [{"file": "...", "line": <int|null>, "problem": "...", '
        '"remedy": "...", "confidence": <1..10>}]}\n\n'
        "DIFF:\n"
        f"{code_diff}\n"
    )


def _loads(text: str) -> dict:
    """Parse a JSON object from judge output, tolerating ```json fences and surrounding prose."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            raise GradeError(f"judge output has no JSON object: {s[:200]!r}") from None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            raise GradeError(f"judge output is not valid JSON: {s[:200]!r}") from exc


def parse_judge_output(text: str) -> tuple[dict[str, float], list[Defect]]:
    """Parse raw judge text into axis scores + defects. Raises :class:`GradeError` if malformed."""
    data = _loads(text)
    raw_scores = data.get("axis_scores")
    if not isinstance(raw_scores, dict):
        raise GradeError("judge output missing 'axis_scores' object")
    scores: dict[str, float] = {}
    for name, val in raw_scores.items():
        try:
            scores[str(name)] = float(val)
        except (TypeError, ValueError) as exc:
            raise GradeError(f"axis {name!r} score not numeric: {val!r}") from exc

    defects: list[Defect] = []
    for d in data.get("defects") or []:
        try:
            confidence = int(d.get("confidence"))
        except (TypeError, ValueError, AttributeError) as exc:
            raise GradeError(f"defect confidence not an int: {d!r}") from exc
        line = d.get("line")
        defects.append(Defect(
            problem=str(d.get("problem") or ""),
            remedy=str(d.get("remedy") or ""),
            confidence=confidence,
            file=str(d.get("file") or ""),
            line=int(line) if isinstance(line, (int, float)) else None,
        ))
    return scores, defects


def grade(complete: Complete, rubric: Rubric, code_diff: str) -> Verdict:
    """Grade one code-state against one rubric with a fresh-context judge. PURE math in
    :func:`agent_factory.rubric.evaluate`; this only drives the model and parses its output."""
    text = complete(build_judge_prompt(rubric, code_diff))
    scores, defects = parse_judge_output(text)
    return evaluate(rubric, scores, defects)
