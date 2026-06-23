"""Convert Matthew's distillation/scoring output into candidate-api-v1 records.

The adapter reads structured pipeline insights (``Insight`` JSON), runs them
through the vector graph write path, and emits dashboard-shaped candidates with
confidence breakdown and contradiction links derived from graph flags.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from knowledge.injestion.injestion_def import Insight
from knowledge.knowledge_graph.knowledge_graph_def import Contradiction, Fact
from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import VectorGraph

HERE = Path(__file__).parent
DEFAULT_INSIGHTS = HERE / "data" / "pipeline-insights.json"
DEFAULT_EXPORT = HERE / "data" / "pipeline-candidates.json"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _title_from_text(text: str) -> str:
    first = text.strip().split("\n", 1)[0]
    sentence = re.split(r"[.!?]\s", first, maxsplit=1)[0].strip()
    return (sentence or first)[:120]


def _confidence_breakdown(fact: Fact) -> dict[str, Any]:
    frequency = min(1.0, fact.observation_count / 10.0)
    recency = min(1.0, max(0.0, fact.confidence))
    breadth = 0.75 if fact.scope in (None, "", "global") else 0.55
    return {
        "frequency": round(frequency, 2),
        "recency": round(recency, 2),
        "breadth": round(breadth, 2),
        "frequencyRationale": f"Observed {fact.observation_count} time(s) in session logs",
        "recencyRationale": "Derived from pipeline confidence score",
        "breadthRationale": f"Scope: {fact.scope or 'global'}",
    }


def fact_to_candidate(
    fact: Fact,
    *,
    state: str = "proposed",
    rival_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Map a stored ``Fact`` to the dashboard candidate read model."""
    provenance = fact.source or f"pipeline/fact:{fact.id}"
    candidate: dict[str, Any] = {
        "id": f"pipe_{fact.id[:12]}",
        "title": _title_from_text(fact.text),
        "content": fact.text,
        "state": state,
        "confidence": round(min(1.0, max(0.0, fact.confidence)), 2),
        "provenance": provenance,
        "createdAt": _now(),
        "confidenceBreakdown": _confidence_breakdown(fact),
        "auditTrail": [
            {
                "action": "distilled",
                "timestamp": _now(),
                "provenance": provenance,
                "actor": "pipeline",
            },
            {
                "action": "scored",
                "timestamp": _now(),
                "provenance": provenance,
                "actor": "pipeline",
            },
        ],
    }
    if rival_ids:
        candidate["contradiction_ids"] = sorted(set(rival_ids))
    if fact.category:
        candidate["category"] = fact.category
    if fact.scope:
        candidate["scope"] = fact.scope
    return candidate


def _rival_map(contradictions: list[Contradiction]) -> dict[str, list[str]]:
    links: dict[str, set[str]] = {}
    for pair in contradictions:
        a, b = pair.flagged.id, pair.conflicting.id
        links.setdefault(a, set()).add(b)
        links.setdefault(b, set()).add(a)
    return {k: sorted(v) for k, v in links.items()}


def candidates_from_graph(graph: VectorGraph) -> list[dict[str, Any]]:
    """Export all facts in ``graph`` as candidate-api records."""
    rivals = _rival_map(graph.contradictions())
    out: list[dict[str, Any]] = []
    for fact in graph.facts:
        # The fact's own lifecycle state (proposed/active/decayed) is the
        # candidate state — set at write time by the approval decision.
        out.append(
            fact_to_candidate(
                fact,
                state=fact.state,
                rival_ids=rivals.get(fact.id),
            )
        )
    return out


def ingest_insights(graph: VectorGraph, insights: list[Insight]) -> None:
    """Write distilled insights through the vector graph policy pipeline."""
    for insight in insights:
        before = len(graph.facts)
        graph.write(insight.raw_text)
        if len(graph.facts) <= before:
            continue
        fact = graph.facts[-1]
        if insight.source is not None:
            fact.source = insight.source
        fact.confidence = insight.confidence
        if insight.scope is not None:
            fact.scope = insight.scope
        if insight.category is not None:
            fact.category = insight.category
        fact.observation_count = insight.observation_count


def load_insights(path: Path = DEFAULT_INSIGHTS) -> list[Insight]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"expected insight list in {path}")
    return [Insight.model_validate(item) for item in raw]


def export_pipeline_candidates(
    *,
    insights_path: Path = DEFAULT_INSIGHTS,
    output_path: Path = DEFAULT_EXPORT,
) -> list[dict[str, Any]]:
    """Ingest pipeline insights and write candidate-api JSON for ``CandidateStore``."""
    graph = VectorGraph()
    insights = load_insights(insights_path)
    ingest_insights(graph, insights)
    candidates = candidates_from_graph(graph)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    return candidates
