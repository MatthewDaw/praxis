"""Generate dashboard mock candidates from registered knowledge/evals/cases YAML."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from knowledge.evals.eval_def import EvalCase

# Demo narrative rows in mock_data.py already cover these case ids (incl. rivals).
HAND_CRAFTED_EVAL_CASE_IDS = frozenset(
    {
        "quirky_exhaustive_switch",
        "quirky_config_load_order",
        "pathlib_preference",
        "poison_negative_control_good",
        "poison_negative_control_bad",
        "promote_then_rerun",
    }
)

_DEFAULT_TIMESTAMP = "2026-06-18T12:00:00Z"


def namespace_from_case(source_dir: str | None, case_id: str) -> str:
    """Map a case to matt / monica / quirky for dashboard grouping."""
    if source_dir:
        parts = {part.lower() for part in Path(source_dir).parts}
        if "matt" in parts:
            return "matt"
        if "monica" in parts:
            return "monica"
        if "quirky" in parts:
            return "quirky"
    if case_id.startswith("quirky_"):
        return "quirky"
    return "eval"


def humanize_case_id(case_id: str) -> str:
    return case_id.replace("_", " ").strip().title()


def insight_lines(case: EvalCase) -> list[str]:
    lines: list[str] = []
    for text in case.seeded_insight.direct_to_graph:
        stripped = (text or "").strip()
        if stripped:
            lines.append(stripped)
    for text in case.seeded_insight.via_ingestor:
        stripped = (text or "").strip()
        if stripped:
            lines.append(stripped)
    return lines


def fallback_content(case: EvalCase) -> str:
    prompt = (case.seed_prompt or "").strip()
    if prompt:
        first_line = prompt.split("\n", 1)[0].strip()
        if first_line:
            return first_line[:500]
    return f"Seeded lesson for eval case {case.id}."



def generate_eval_candidate_dicts(
    skip_case_ids: frozenset[str] | None = None,
) -> list[dict]:
    """Build one contract-shaped mock row per registered eval case."""
    from knowledge.evals.run import load_cases

    skip = skip_case_ids or HAND_CRAFTED_EVAL_CASE_IDS
    rows: list[dict] = []

    for case in load_cases():
        if case.id in skip:
            continue

        insights = insight_lines(case)
        if not insights:
            content = fallback_content(case)
        elif len(insights) == 1:
            content = insights[0]
        else:
            content = "\n\n".join(insights)

        ns = namespace_from_case(case.source_dir, case.id)
        provenance = f"logs/evals/{ns}/{case.id}.jsonl:1"
        confidence = 0.78

        row: dict = {
            "id": f"eval_{case.id}",
            "evalCaseId": case.id,
            "evalCaseNamespace": ns,
            "title": humanize_case_id(case.id),
            "content": content,
            "state": "proposed",
            "confidence": confidence,
            "provenance": provenance,
            "createdAt": _DEFAULT_TIMESTAMP,
            "scope": f"eval/{ns}",
            "category": "eval_fixture",
            "confidenceBreakdown": {
                "frequency": round(confidence - 0.05, 2),
                "recency": round(confidence + 0.03, 2),
                "breadth": round(confidence - 0.02, 2),
                "frequencyRationale": f"Registered eval case under cases/{ns}/",
                "recencyRationale": "Auto-generated from eval case registry for mock dashboard",
                "breadthRationale": f"Exercises eval harness case {case.id}",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": _DEFAULT_TIMESTAMP,
                    "provenance": provenance,
                    "actor": "eval-mock-bridge",
                },
            ],
        }
        if len(insights) > 1:
            row["evalCaseInsightCount"] = len(insights)
        rows.append(row)

    return rows
