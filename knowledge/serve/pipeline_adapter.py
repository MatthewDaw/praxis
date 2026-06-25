"""Convert Matthew's distillation/scoring output into candidate-api-v1 records.

The adapter reads structured pipeline insights (``Insight`` JSON), runs them
through the vector graph write path, and emits dashboard-shaped candidates with
confidence breakdown and contradiction links derived from graph flags.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
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
    state: str | None = None,
    rival_ids: list[str] | None = None,
    rivals: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Map a stored ``Fact`` to the dashboard candidate read model.

    The candidate id IS the raw fact id — the candidate/contradiction routes now
    project directly off the facts spine, so there is no separate id namespace to
    translate. Contradiction rival ids are likewise raw fact ids.

    Contradiction links are emitted two ways: the flat ``contradiction_ids`` (kept
    for back-compat) and the richer ``contradictions`` ``[{id, status}]`` where
    ``status`` is ``pending`` | ``resolved`` (FR-012). Callers pass either ``rivals``
    (``{id: status}``) or the legacy ``rival_ids`` (treated as all ``pending``).
    """
    provenance = fact.source or f"pipeline/fact:{fact.id}"
    meta = fact.meta or {}
    if "auditTrail" in meta:
        audit_trail = meta["auditTrail"]
    else:
        audit_trail = [
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
        ]
    candidate: dict[str, Any] = {
        "id": fact.id,
        "title": meta.get("title") or _title_from_text(fact.text),
        "content": fact.text,
        "state": state if state is not None else fact.state,
        "confidence": round(min(1.0, max(0.0, fact.confidence)), 2),
        "provenance": provenance,
        "createdAt": fact.created_at or _now(),
        "confidenceBreakdown": _confidence_breakdown(fact),
        "auditTrail": audit_trail,
    }
    rival_status: dict[str, str] = dict(rivals) if rivals else {}
    if rival_ids:
        for rid in rival_ids:
            rival_status.setdefault(rid, "pending")
    if rival_status:
        ids = sorted(rival_status)
        candidate["contradiction_ids"] = ids
        candidate["contradictions"] = [
            {"id": rid, "status": rival_status[rid]} for rid in ids
        ]
    if fact.category:
        candidate["category"] = fact.category
    if fact.scope:
        candidate["scope"] = fact.scope
    # H12: surface the persisted ``meta`` jsonb so writer-supplied fields round-trip
    # on the candidate read model (``/context`` stays lean; ``/candidates`` is the
    # detail view). ``meta`` already carries dashboard-internal keys (title,
    # auditTrail, claim) alongside writer fields — expose it whole.
    if meta:
        candidate["meta"] = dict(meta)
    if fact.cluster_id is not None:
        candidate["cluster_id"] = fact.cluster_id
    if fact.cluster_label:
        candidate["cluster_label"] = fact.cluster_label
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
        # The fact's own lifecycle state IS the candidate state, and it already
        # encodes incumbency: a directly-approved fact is ``active`` (an established
        # incumbent that keeps its place in the graph), while a fact distilled
        # through ingestion is ``proposed`` (a newcomer, staged for review). When a
        # newcomer contradicts an incumbent, only the newcomer is ``proposed`` and so
        # only it surfaces in the Contradictions tab; the incumbent stays active. Two
        # newcomers that clash are both ``proposed`` — both held. No special-casing
        # needed here: don't demote, just pass the state through.
        out.append(
            fact_to_candidate(fact, state=fact.state, rival_ids=rivals.get(fact.id))
        )
    return out


@dataclass
class IngestReport:
    """Inline rows-in vs facts-out reconciliation for one ingest call (§3a).

    Every submitted insight lands in exactly one bucket, so the accounting
    invariant always holds::

        facts_active + len(merged_into_existing) + rejected == rows_submitted

    - ``facts_active`` — submitted rows that produced a *new* stored fact (action
      ``add``/``overwrite``). Named "active" for the report contract; the fact's own
      lifecycle state is whatever ``write`` seeded (``proposed`` by default).
    - ``merged_into_existing`` — the ``update_target_id`` of each row the deduper
      folded into an existing fact (action ``update``). This is where silently-
      dropped sibling rows would show up as a too-short ``facts_active`` count, so
      the caller can audit which incumbents absorbed incoming rows.
    - ``rejected`` — rows the pipeline dropped (``decision.dropped``) or that yielded
      no fact at all (empty text → ``write`` returns ``None``). These are the
      "audit the rejected pile" rows the candidate surface (``/candidates?state=
      rejected``) lets a reviewer inspect.
    """

    rows_submitted: int = 0
    facts_active: int = 0
    merged_into_existing: list[str] = field(default_factory=list)
    rejected: int = 0

    @property
    def accounted_for(self) -> bool:
        """The completeness invariant: every submitted row landed in a bucket."""
        return (
            self.facts_active + len(self.merged_into_existing) + self.rejected
            == self.rows_submitted
        )

    def to_dict(self) -> dict[str, Any]:
        """The wire shape surfaced in the ingest HTTP response (§3a)."""
        return {
            "rows_submitted": self.rows_submitted,
            "facts_active": self.facts_active,
            "merged_into_existing": list(self.merged_into_existing),
            "rejected": self.rejected,
        }


def ingest_insights(graph: VectorGraph, insights: list[Insight]) -> IngestReport:
    """Write distilled insights through the vector graph policy pipeline.

    Returns an :class:`IngestReport` reconciling rows-in vs facts-out so a caller
    can verify ingestion didn't silently drop rows. Each write's enacted
    ``WriteDecision`` tells us the per-row outcome (a new fact, a merge into an
    existing one, or a drop) without diffing ``facts`` before/after.
    """
    report = IngestReport(rows_submitted=len(insights))
    for insight in insights:
        decision = graph.write(insight.raw_text)
        # Empty/whitespace text writes nothing (``write`` returns None); a step may
        # also have suppressed the write entirely — both are rejected/dropped rows.
        if decision is None or decision.dropped:
            report.rejected += 1
            continue
        # A merge bumped an existing fact in place: record which incumbent absorbed
        # this row. No new fact was created, so there's nothing to post-process.
        if decision.action == "update" and decision.update_target_id:
            report.merged_into_existing.append(decision.update_target_id)
            continue
        # A fresh fact was appended (add / overwrite): stamp the insight's metadata
        # onto it via the id the store recorded on the decision.
        report.facts_active += 1
        fact = _fact_by_id(graph, decision.added_fact_id)
        if fact is None:
            continue
        if insight.source is not None:
            fact.source = insight.source
        fact.confidence = insight.confidence
        if insight.scope is not None:
            fact.scope = insight.scope
        if insight.category is not None:
            fact.category = insight.category
        fact.observation_count = insight.observation_count
    return report


def _fact_by_id(graph: VectorGraph, fact_id: str | None) -> Fact | None:
    if fact_id is None:
        return None
    for fact in graph.facts:
        if fact.id == fact_id:
            return fact
    return None


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
