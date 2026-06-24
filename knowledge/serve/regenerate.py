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
class FactSeed:
    """A pure fact-seed the eval-cache orchestrator writes into the eval graph.

    Carries only what ``graph.write`` needs; embedding + contradiction edges are
    the graph's job. ``state`` is ``"proposed"`` (via_ingestor) or ``"active"``
    (direct_to_graph), mirroring the channel-to-state mapping used elsewhere.
    """

    text: str
    state: str
    source: str | None = None
    scope: str | None = None
    category: str | None = None


def case_ids_for(scopes: list[str], case_ids: list[str]) -> list[str]:
    """Resolve folder scopes and/or explicit case ids into a deduped, ordered list.

    A scope is a cases-subdir relative to ``CASES_ROOT`` (e.g. ``"matt/applications"``,
    or ``"."`` for the whole tree). When both ``scopes`` and ``case_ids`` are empty,
    return every case id under ``CASES_ROOT``. When explicit ``case_ids`` are given,
    the result is restricted to those (intersected with the scope-resolved set).
    """
    # A bare ``graph:<dir>`` preset string is tolerated as a scope too (mapped via
    # ``_graph_scope``); plain relative dirs pass through unchanged. Default to the
    # whole cases tree when no folder scopes are given.
    scope_dirs = [(_graph_scope(s) or s) for s in scopes] or ["."]
    resolved: list[str] = []
    seen: set[str] = set()
    for relative in scope_dirs:
        for case in load_cases(_resolve_cases_dir(relative)):
            if case.id not in seen:
                seen.add(case.id)
                resolved.append(case.id)
    if case_ids:
        wanted = set(case_ids)
        return [cid for cid in resolved if cid in wanted]
    return resolved


def distill_case(case_id: str, *, distill: bool) -> list[FactSeed]:
    """Load a single case by id and return its fact seeds (no DB, no embedding).

    Each ``via_ingestor`` row maps to state ``"proposed"``; each ``direct_to_graph``
    row maps to state ``"active"``. ``source`` is ``f"evals/{case_id}"``;
    ``scope``/``category`` are left ``None`` (cases don't provide them today).

    ``distill=False`` (fast): passthrough — split each seed source into lines, keep
    only ``_is_real_fact`` lines, one ``FactSeed`` per kept line.
    ``distill=True``: run the real distillation via ``gpt-4o-mini`` over an
    ``InMemoryGraph`` ingestor and collect the resulting distilled fact texts.
    Raises ``RegenerateUnavailableError`` if ``OPENROUTER_API_KEY`` is unset.
    """
    from knowledge.knowledge_graph.knowledge_graph_variants.in_memory_graph import InMemoryGraph
    from knowledge.injestion.injestor_variants.prompt_injestor import PromptIngestor

    case = next((c for c in load_cases(CASES_ROOT) if c.id == case_id), None)
    if case is None:
        raise ValueError(f"no such eval case: {case_id!r}")

    source = f"evals/{case_id}"
    seeds: list[FactSeed] = []

    # ``direct_to_graph`` seeds mirror a real run's ``graph.write(text, state="active")``
    # (see knowledge/evals/run.py ``_seed_knowledge``): each line is written VERBATIM,
    # bypassing the ingestor/distiller, one fact per seed entry. Running these through
    # distillation corrupts curated text (e.g. "RULE_B second" -> "RULE_B is a specific
    # rule or guideline.") and is the channel the cache must reproduce faithfully.
    for text in case.seeded_insight.direct_to_graph:
        fact_text = " ".join(str(text).split())
        if fact_text:
            seeds.append(FactSeed(text=fact_text, state="active", source=source))

    # ``via_ingestor`` seeds mirror ``ingestor.ingest(text)``: the real distillation
    # path (LLM distill when requested, deterministic passthrough otherwise), landing
    # as ``proposed``. This is the only channel that should ever be distilled.
    via_rows = [str(t) for t in case.seeded_insight.via_ingestor if str(t).strip()]
    if via_rows:
        if distill:
            if not os.getenv("OPENROUTER_API_KEY"):
                raise RegenerateUnavailableError(
                    "distillation needs OPENROUTER_API_KEY set on the API"
                )
            from knowledge.llm.llm_def import ChatMessage
            from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm

            llm = OpenRouterLlm(model="openai/gpt-4o-mini")
            ingestor = PromptIngestor(
                InMemoryGraph(),
                llm=lambda prompt: llm.complete([ChatMessage(role="user", content=prompt)]),
            )
            keep = bool
        else:
            ingestor = PromptIngestor(InMemoryGraph())  # no llm => line split
            keep = _is_real_fact
        for text in via_rows:
            for insight in ingestor.synthesis(text):
                fact_text = " ".join(insight.raw_text.split())
                if keep(fact_text):
                    seeds.append(FactSeed(text=fact_text, state="proposed", source=source))
    return seeds


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


def _seed_write_graph() -> tuple[VectorGraph, Any]:
    """The graph that seed-loading writes into, plus the ingest LLM (None offline).

    With ``OPENROUTER_API_KEY`` set, use the real embedder (so semantically-related
    seeds clear the recall floor and reach dedup) and the structural contradiction
    path on ``gpt-4o-mini`` -- mirroring the vector store's ``default_write_policy``.
    The contradiction flags it records become the candidates' ``contradiction_ids``,
    which is exactly what the dashboard's Contradictions tab renders. Without a key,
    fall back to the deterministic offline ``FakeEmbedder`` and no judge (no network,
    and hence no contradiction flags -- fake vectors can't surface recall candidates).
    """
    if os.getenv("OPENROUTER_API_KEY"):
        from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import (
            default_write_policy,
        )
        from knowledge.llm.embedder_variants.openrouter_embedder import OpenRouterEmbedder
        from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm

        llm = OpenRouterLlm(model="openai/gpt-4o-mini")
        graph = VectorGraph(embedder=OpenRouterEmbedder(), policy=default_write_policy(llm))
        return graph, llm

    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder

    return VectorGraph(embedder=FakeEmbedder(), policy=[Redactor(), Deduper()]), None


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

    # Collect each unique seeded source once, split by channel. Mirrors a real run
    # (and ``distill_case``): ``direct_to_graph`` seeds are written VERBATIM as active
    # facts (never distilled/split — that corrupts curated text and can split one
    # "A or B" line into two facts that then false-flag as contradictory); only
    # ``via_ingestor`` goes through the ingestor and lands proposed.
    seen: set[str] = set()
    direct_seeds: list[str] = []  # -> graph.write(text, state="active"), verbatim
    via_seeds: list[str] = []  # -> ingestor, state="proposed"
    for case in cases:
        for bucket, rows in (
            (direct_seeds, case.seeded_insight.direct_to_graph),
            (via_seeds, case.seeded_insight.via_ingestor),
        ):
            for text in rows:
                normalized = str(text).strip()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    bucket.append(text)

    # Real embedder + ConflictFlagger when a key is present (so contradictions land on
    # the candidates the UI reads), else the offline FakeEmbedder.
    graph, ingest_llm = _seed_write_graph()

    # direct_to_graph: one verbatim active fact per seed entry (collapse whitespace only).
    for source in direct_seeds:
        text = " ".join(str(source).split())
        if text:
            graph.write(text, state="active")

    # via_ingestor: the real distillation path. ``distill`` only changes how the seed
    # *text* enters the graph (LLM-distilled vs. verbatim line-split); both land proposed.
    if via_seeds:
        if config.distill:
            if ingest_llm is None:
                raise RegenerateUnavailableError(
                    "distillation needs OPENROUTER_API_KEY set on the API"
                )
            from knowledge.llm.llm_def import ChatMessage

            ingestor = PromptIngestor(
                graph,
                llm=lambda prompt: ingest_llm.complete([ChatMessage(role="user", content=prompt)]),
            )
            for source in via_seeds:
                ingestor.ingest(source, state="proposed")
        else:
            from knowledge.knowledge_graph.knowledge_graph_variants.in_memory_graph import InMemoryGraph

            # Passthrough split, but drop header/fragment lines (e.g. "Cohorts:",
            # "Relevant Skills") so the graph holds real facts, not section headers.
            splitter = PromptIngestor(InMemoryGraph())  # no llm => line split
            for source in via_seeds:
                for insight in splitter.synthesis(source):
                    text = " ".join(insight.raw_text.split())
                    if _is_real_fact(text):
                        graph.write(text, state="proposed")

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
