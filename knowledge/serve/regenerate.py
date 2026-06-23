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
# Fixed presets plus the dynamic ``graph:<relative-dir>`` family (see
# ``_graph_scope``): a graph preset runs the REAL graph over every case under
# ``knowledge/evals/cases/<relative-dir>`` — distilling each case's source docs
# through the live ingestor + embedder and exporting the facts as candidates.
# "matt-applications" is kept as a friendly alias for ``graph:matt/applications``.
SUPPORTED_PRESETS = {"offline-fake", "openrouter", "matt-applications"}
GRAPH_PRESET_PREFIX = "graph:"
CASES_ROOT = Path(__file__).resolve().parents[1] / "evals" / "cases"
MATT_CASES_DIR = CASES_ROOT / "matt"
MATT_APPLICATIONS_DIR = MATT_CASES_DIR / "applications"


def _graph_scope(preset: str) -> str | None:
    """Return the cases-dir (relative to ``CASES_ROOT``) a graph preset targets.

    ``graph:matt/applications`` -> ``"matt/applications"``; the ``matt-applications``
    alias -> the same. ``graph:`` / ``graph:.`` -> ``"."`` (the whole cases tree).
    Returns ``None`` for non-graph presets.
    """
    if preset == "matt-applications":
        return "matt/applications"
    if preset.startswith(GRAPH_PRESET_PREFIX):
        return preset[len(GRAPH_PRESET_PREFIX):].strip() or "."
    return None


class RegenerateUnavailableError(RuntimeError):
    """Raised when a requested regeneration preset is intentionally unavailable."""


def _is_real_fact(text: str) -> bool:
    """Filter out passthrough-split noise: too-short lines and bare section headers
    (e.g. "Cohorts:", "Relevant Skills") that aren't real facts."""
    if len(text) < 25:
        return False
    if text.endswith(":") and len(text.split()) <= 4:
        return False
    return True


@dataclass(frozen=True)
class PipelineConfig:
    """API-facing regeneration config.

    ``offline-fake`` is deterministic and credit-free. ``openrouter`` is modeled
    but deliberately guarded by an env flag so the dashboard cannot start paid or
    long-running work by accident.
    """

    preset: str = DEFAULT_PRESET
    case_ids: tuple[str, ...] = field(default_factory=tuple)
    # Explicit cases-folder scopes to ingest into the graph (multi-select). When
    # set, these win over ``preset`` — the graph is built from exactly these dirs.
    scopes: tuple[str, ...] = field(default_factory=tuple)
    # Run the real distillation pipeline (LLM + embeddings) vs. a fast offline
    # read of the seed text. False => "use the data" (file retrieval, instant).
    distill: bool = False

    @classmethod
    def from_body(cls, body: dict[str, Any] | None) -> "PipelineConfig":
        body = body or {}
        raw_scopes = body.get("scopes")
        scopes = (
            tuple(str(s) for s in raw_scopes if str(s).strip())
            if isinstance(raw_scopes, list)
            else ()
        )
        preset = str(body.get("preset") or DEFAULT_PRESET).strip() or DEFAULT_PRESET
        # ``graph:<dir>`` presets are dynamic (any cases subdir); the rest are fixed.
        # Skip preset validation when explicit scopes are given (they drive the run).
        if not scopes and preset not in SUPPORTED_PRESETS and _graph_scope(preset) is None:
            raise ValueError(
                f"unsupported regenerate preset {preset!r}; expected one of "
                f"{sorted(SUPPORTED_PRESETS)} or 'graph:<cases-subdir>'"
            )
        raw_case_ids = body.get("caseIds") or body.get("case_ids") or []
        if isinstance(raw_case_ids, str):
            case_ids = (raw_case_ids,)
        elif isinstance(raw_case_ids, list):
            case_ids = tuple(str(item) for item in raw_case_ids if str(item).strip())
        else:
            case_ids = ()
        return cls(
            preset=preset, case_ids=case_ids, scopes=scopes, distill=bool(body.get("distill"))
        )


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
    if config.scopes:
        return _regenerate_from_cases(list(config.scopes), config)
    scope = _graph_scope(config.preset)
    if scope is not None:
        return _regenerate_from_cases([scope], config)
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


def _resolve_cases_dir(relative: str) -> Path:
    """Resolve a cases subdir under ``CASES_ROOT``, blocking path traversal."""
    root = CASES_ROOT.resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"cases scope {relative!r} must stay under knowledge/evals/cases")
    if not target.is_dir():
        raise ValueError(f"no such cases directory: {relative!r}")
    return target


def _regenerate_from_cases(relative_dirs: list[str], config: PipelineConfig) -> RegenerationResult:
    """Build the graph from every case under the selected folders.

    Two modes, decided by ``config.distill``:
      * ``False`` (default) — fast offline read: each case's seeded source text is
        written straight into the graph (passthrough ingestor + offline
        ``FakeEmbedder``). No LLM, no network — basically file retrieval.
      * ``True`` — run the real pipeline: distill each source via ``gpt-4o-mini``
        and embed with the live OpenRouter embedder (slow, uses credits).

    ``via_ingestor`` sources land ``proposed`` (passive add); ``direct_to_graph``
    sources land ``active`` (direct approval), matching the channel mapping.
    """
    from knowledge.evals.run import load_cases
    from knowledge.injestion.injestor_variants.prompt_injestor import PromptIngestor

    # Gather cases across all selected folders, deduped by id.
    seen_ids: set[str] = set()
    cases = []
    for relative_dir in relative_dirs or ["."]:
        for case in load_cases(_resolve_cases_dir(relative_dir)):
            if case.id not in seen_ids:
                seen_ids.add(case.id)
                cases.append(case)
    if config.case_ids:
        wanted = set(config.case_ids)
        cases = [c for c in cases if c.id in wanted]

    # Collect each unique seeded source once, tagged with the state its channel
    # implies: via_ingestor -> proposed, direct_to_graph -> active.
    seen: set[str] = set()
    seeds: list[tuple[str, str]] = []  # (source_text, state)
    for case in cases:
        for state, rows in (
            ("proposed", case.seeded_insight.via_ingestor),
            ("active", case.seeded_insight.direct_to_graph),
        ):
            for text in rows:
                normalized = str(text).strip()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    seeds.append((text, state))

    if config.distill:
        if not os.getenv("OPENROUTER_API_KEY"):
            raise RegenerateUnavailableError(
                "distillation needs OPENROUTER_API_KEY set on the API"
            )
        from knowledge.llm.embedder_variants.openrouter_embedder import OpenRouterEmbedder
        from knowledge.llm.llm_def import ChatMessage
        from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm

        ingest_model = OpenRouterLlm(model="openai/gpt-4o-mini")
        graph = VectorGraph(embedder=OpenRouterEmbedder(), policy=[Redactor(), Deduper()])
        ingestor = PromptIngestor(
            graph,
            llm=lambda prompt: ingest_model.complete([ChatMessage(role="user", content=prompt)]),
        )
        for source, state in seeds:
            ingestor.ingest(source, state=state)
    else:
        from knowledge.knowledge_graph.knowledge_graph_variants.in_memory_graph import InMemoryGraph
        from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder

        graph = VectorGraph(embedder=FakeEmbedder(), policy=[Redactor(), Deduper()])
        # Passthrough split, but drop header/fragment lines (e.g. "Cohorts:",
        # "Relevant Skills") so the graph holds real facts, not section headers.
        splitter = PromptIngestor(InMemoryGraph())  # no llm => line split
        for source, state in seeds:
            for insight in splitter.synthesis(source):
                text = " ".join(insight.raw_text.split())
                if _is_real_fact(text):
                    graph.write(text, state=state)

    # Tag facts with topic clusters (embed -> reduce -> HDBSCAN -> LLM labels) so the
    # graph view can group them into labeled super-nodes. Best-effort: degrades to
    # an unclustered graph if embeddings are unavailable.
    try:
        from knowledge.knowledge_graph.clustering import assign_clusters

        assign_clusters(graph.facts)
    except Exception:
        pass

    # Each stored fact is one distilled insight (reported in the UI toast).
    insights = [
        Insight(raw_text=fact.text, source=fact.source, scope=fact.scope, category=fact.category)
        for fact in graph.facts
    ]
    candidates = candidates_from_graph(graph)
    return RegenerationResult(
        preset=config.preset,
        ran_at=_now(),
        cases_run=len(cases),
        cases_skipped=0,
        insights=insights,
        candidates=candidates,
        eval_results=[],
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
