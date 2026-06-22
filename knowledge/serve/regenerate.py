"""In-process eval regeneration for candidate-api-v1."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from knowledge.evals.run import (
    FakeRunner,
    load_cases,
    partition_by_capability,
    run_case_full,
    status_of,
)
from knowledge.injestion.injestion_def import Insight
from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import VectorGraph
from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
from knowledge.serve.pipeline_adapter import candidates_from_graph, ingest_insights

DEFAULT_PRESET = "offline-fake"
SUPPORTED_PRESETS = {"offline-fake", "openrouter"}
MATT_CASES_DIR = Path(__file__).resolve().parents[1] / "evals" / "cases" / "matt"


class RegenerateUnavailableError(RuntimeError):
    """Raised when a requested regeneration preset is intentionally unavailable."""


@dataclass(frozen=True)
class PipelineConfig:
    """API-facing regeneration config.

    ``offline-fake`` is deterministic and credit-free. ``openrouter`` is modeled
    but deliberately guarded by an env flag so the dashboard cannot start paid or
    long-running work by accident.
    """

    preset: str = DEFAULT_PRESET
    case_ids: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_body(cls, body: dict[str, Any] | None) -> "PipelineConfig":
        body = body or {}
        preset = str(body.get("preset") or DEFAULT_PRESET).strip() or DEFAULT_PRESET
        if preset not in SUPPORTED_PRESETS:
            raise ValueError(
                f"unsupported regenerate preset {preset!r}; expected one of {sorted(SUPPORTED_PRESETS)}"
            )
        raw_case_ids = body.get("caseIds") or body.get("case_ids") or []
        if isinstance(raw_case_ids, str):
            case_ids = (raw_case_ids,)
        elif isinstance(raw_case_ids, list):
            case_ids = tuple(str(item) for item in raw_case_ids if str(item).strip())
        else:
            case_ids = ()
        return cls(preset=preset, case_ids=case_ids)


@dataclass(frozen=True)
class RegenerationResult:
    preset: str
    ran_at: str
    cases_run: int
    cases_skipped: int
    insights: list[Insight]
    candidates: list[dict[str, Any]]
    eval_results: list[dict[str, Any]]


def regenerate_candidates(config: PipelineConfig) -> RegenerationResult:
    """Run the supported eval preset and export fresh pipeline candidates."""
    if config.preset == "openrouter" and os.getenv("PRAXIS_REGENERATE_OPENROUTER") != "1":
        raise RegenerateUnavailableError(
            "openrouter regeneration is disabled; set PRAXIS_REGENERATE_OPENROUTER=1 on the API to enable it"
        )
    if config.preset != DEFAULT_PRESET:
        raise RegenerateUnavailableError(
            f"regenerate preset {config.preset!r} is not implemented by this API"
        )

    cases = _select_cases(config)
    runner = FakeRunner()
    runnable, skipped = partition_by_capability(cases, runner)
    eval_results = [_run_case(case, runner) for case in runnable]

    insights = _insights_from_cases(runnable)
    graph = VectorGraph(policy=[Redactor(), Deduper()])
    ingest_insights(graph, insights)
    candidates = candidates_from_graph(graph)

    return RegenerationResult(
        preset=config.preset,
        ran_at=_now(),
        cases_run=len(runnable),
        cases_skipped=len(skipped),
        insights=insights,
        candidates=candidates,
        eval_results=eval_results,
    )


def _select_cases(config: PipelineConfig):
    cases = load_cases(MATT_CASES_DIR)
    if config.case_ids:
        wanted = set(config.case_ids)
        return [case for case in cases if case.id in wanted]
    return [case for case in cases if case.component is not None]


def _run_case(case, runner: FakeRunner) -> dict[str, Any]:
    _, _, result = run_case_full(case, runner)
    return {
        "case_id": case.id,
        "status": status_of(result),
        "checks_passed": sum(check.passed for check in result.checks),
        "checks_total": len(result.checks),
        "xfail_reason": result.xfail_reason,
    }


def _insights_from_cases(cases) -> list[Insight]:
    seen: set[str] = set()
    insights: list[Insight] = []
    for case in cases:
        for source_kind, rows in (
            ("via_ingestor", case.seeded_insight.via_ingestor),
            ("direct_to_graph", case.seeded_insight.direct_to_graph),
        ):
            for index, text in enumerate(rows, start=1):
                normalized = " ".join(str(text).split())
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                insights.append(
                    Insight(
                        raw_text=normalized,
                        source=f"evals/{case.id}:{source_kind}:{index}",
                        confidence=0.82,
                        scope=f"evals/matt/{case.component or 'full'}",
                        category="eval_seed",
                    )
                )
    return insights


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
