"""LLM-backed evaluator + refuter for the coverage engine.

See ``docs/coverage-spine/05-coverage-engine.md``. The engine in :mod:`coverage` takes
injected ``item_evaluator`` / ``refuter`` callables; this module builds those from an LLM
**completion function**::

    Complete = Callable[[str], str]   # prompt -> raw model text

Decoupling from any specific SDK keeps the module testable (inject a canned ``Complete``),
lets the caller choose the backend (Anthropic SDK via :func:`make_anthropic_complete`, a
Claude Code subagent, or a stub), and concentrates the *forcing* rules in :mod:`coverage`'s
parsers (evidence-required downgrade, default-refuted) rather than in fragile glue here.

Typical wiring::

    from evals.plan_repro.coverage import run_coverage, lexical_related_query
    from evals.plan_repro.llm_evaluator import make_anthropic_complete, make_llm_evaluator, make_llm_refuter

    complete = make_anthropic_complete()                      # real model
    report = run_coverage(
        golden, candidates, lexical_related_query,
        make_llm_evaluator(complete),                         # semantic judge
        refuter=make_llm_refuter(complete),                   # targeted adversarial pass
    )
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from evals.plan_repro.coverage import (
    COVERED,
    MISSING,
    Feature,
    ItemEvaluator,
    PartResult,
    Refuter,
    _overlap,
    _tokens,
    build_judge_prompt,
    build_refuter_prompt,
    judge_result_from_response,
    refuted_from_response,
)

#: A model completion: takes a prompt, returns the raw model text (which should contain JSON).
Complete = Callable[[str], str]

#: Default judge model — capable but cheap; override per call. The refuter may warrant a
#: stronger model than the first-pass judge.
DEFAULT_JUDGE_MODEL = "claude-sonnet-4-6"


# --- tolerant JSON extraction --------------------------------------------------


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of model text — tolerant of ```fences and prose.

    Returns ``{}`` on any failure; the downstream parsers
    (:func:`~evals.plan_repro.coverage.judge_result_from_response` /
    :func:`~evals.plan_repro.coverage.refuted_from_response`) treat an empty/!valid payload
    safely (MISSING / refuted=True), so a malformed judge response never silently passes.
    """
    if not text:
        return {}
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    # Scan for the first balanced {...} object embedded in prose.
    start = s.find("{")
    if start == -1:
        return {}
    depth = 0
    for i in range(start, len(s)):
        ch = s[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start : i + 1])
                    return obj if isinstance(obj, dict) else {}
                except Exception:
                    return {}
    return {}


# --- evaluator / refuter factories ---------------------------------------------


def make_llm_evaluator(complete: Complete) -> ItemEvaluator:
    """Build an ``item_evaluator`` that judges coverage with a model (evidence-required)."""

    def evaluator(part: Feature, related: list[Feature]) -> PartResult:
        raw = complete(build_judge_prompt(part, related))
        data = _extract_json(raw)
        if not data:
            return PartResult(
                part_id=part.id, status=MISSING, derived=part.derived, confidence=1.0,
                notes="unparseable judge response",
            )
        return judge_result_from_response(part, data)

    return evaluator


def make_llm_refuter(complete: Complete) -> Refuter:
    """Build a ``refuter`` that adversarially challenges a claimed-covered match."""

    def refuter(part: Feature, result: PartResult, related: list[Feature]) -> bool:
        raw = complete(build_refuter_prompt(part, result, related))
        return refuted_from_response(_extract_json(raw))

    return refuter


def make_tiered_evaluator(
    complete: Complete, *, lexical_cover_threshold: float = 0.85
) -> ItemEvaluator:
    """Cheap pre-filter, escalate the ambiguous: a near-identical lexical match is taken as
    COVERED without a model call; everything else goes to the LLM judge.

    Safe with targeted-adversarial: a fast-pathed ``derived``/critical part is still selected
    by ``default_adversarial_select`` and re-checked by the (LLM) refuter in ``run_coverage``,
    so the shortcut can't let a wrong match survive on a high-stakes part.
    """
    llm = make_llm_evaluator(complete)

    def evaluator(part: Feature, related: list[Feature]) -> PartResult:
        if related:
            pt = _tokens(part.text)
            score, best = max(
                ((_overlap(pt, _tokens(c.text)), c) for c in related), key=lambda sc: sc[0]
            )
            if score >= lexical_cover_threshold:
                return PartResult(
                    part_id=part.id, status=COVERED, derived=part.derived,
                    evidence=best.text, matched_ids=[best.id], confidence=score,
                    notes="lexical fast-path (near-identical match)",
                )
        return llm(part, related)

    return evaluator


# --- concrete backend: Anthropic SDK -------------------------------------------


def make_anthropic_complete(
    model: str = DEFAULT_JUDGE_MODEL, *, max_tokens: int = 500, api_key: str | None = None
) -> Complete:
    """A :data:`Complete` backed by the Anthropic SDK (imported lazily).

    Raises ``RuntimeError`` if the ``anthropic`` package isn't installed — inject your own
    :data:`Complete` instead (e.g. a stub, or an agent-driven backend) when running without it.
    Reads the API key from ``api_key`` or the standard ``ANTHROPIC_API_KEY`` environment var.
    """
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "anthropic SDK not installed; `pip install anthropic` or inject your own Complete"
        ) from exc

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def complete(prompt: str) -> str:  # pragma: no cover - exercises the network
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            getattr(block, "text", "") for block in msg.content
            if getattr(block, "type", None) == "text"
        )

    return complete
