"""Tests for pipeline → candidate export adapter."""

from pathlib import Path

from knowledge.injestion.injestion_def import Insight
from knowledge.knowledge_graph.knowledge_graph_def import Fact
from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import VectorGraph
from knowledge.serve.pipeline_adapter import (
    candidates_from_graph,
    export_pipeline_candidates,
    fact_to_candidate,
    ingest_insights,
)


def test_fact_to_candidate_shapes_contract_fields():
    fact = Fact(
        id="abc123",
        text="Prefer pathlib over os.path for new Python file operations.",
        source="logs/session_20260616.jsonl:201",
        confidence=0.88,
        scope="global",
        observation_count=7,
    )
    candidate = fact_to_candidate(fact)
    # The candidate id IS the raw fact id now (no pipe_ namespace).
    assert candidate["id"] == "abc123"
    assert candidate["provenance"] == "logs/session_20260616.jsonl:201"
    assert candidate["confidenceBreakdown"]["frequency"] == 0.7
    assert candidate["auditTrail"][0]["actor"] == "pipeline"


def test_fact_to_candidate_honors_meta_title_and_audit_trail():
    fact = Fact(
        id="m1",
        text="some content",
        created_at="2026-06-23T00:00:00Z",
        meta={
            "title": "Custom title",
            "auditTrail": [{"action": "created", "actor": "human-gate"}],
        },
    )
    candidate = fact_to_candidate(fact)
    assert candidate["title"] == "Custom title"
    assert candidate["createdAt"] == "2026-06-23T00:00:00Z"
    assert candidate["auditTrail"] == [{"action": "created", "actor": "human-gate"}]


def test_export_pipeline_candidates_writes_json(tmp_path: Path):
    insights = tmp_path / "insights.json"
    insights.write_text(
        '[{"raw_text": "Use uv run pytest for the test suite.", '
        '"source": "logs/session.jsonl:1", "confidence": 0.9}]',
        encoding="utf-8",
    )
    output = tmp_path / "candidates.json"
    rows = export_pipeline_candidates(insights_path=insights, output_path=output)
    assert len(rows) == 1
    assert output.exists()
    assert rows[0]["content"].startswith("Use uv run pytest")


def test_candidates_from_graph_links_contradictions():
    graph = VectorGraph()
    ingest_insights(
        graph,
        [
            Insight(raw_text="Use explicit error enums in library code."),
            Insight(raw_text="Avoid Box<dyn Error> in public library APIs."),
        ],
    )
    flagged = graph.facts[0]
    flagged.flags.append(f"contradiction:{graph.facts[1].id}")
    candidates = candidates_from_graph(graph)
    assert len(candidates) == 2
    linked = next(c for c in candidates if c["id"] == flagged.id)
    # Rivals are referenced by raw fact id now (candidate id == fact id).
    rival_cid = graph.facts[1].id
    assert linked["contradiction_ids"] == [rival_cid]
    assert any(c["id"] == rival_cid for c in candidates)


def _contradicting_graph(state_first: str, state_second: str) -> VectorGraph:
    """Two contradictory facts written at the given lifecycle states.

    Uses the structural detector: both facts claim the same functional slot
    (timestamp timezone) with incompatible values, so the second write is flagged.
    """
    from knowledge.knowledge_graph.knowledge_graph_def import Claim
    from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
    from knowledge.knowledge_graph.write_policy.write_step_variants import (
        ClaimConflictDetector,
        ClaimValueJudge,
    )
    from knowledge.llm.llm_variants.fake_llm import FakeLlm

    class _Claims(WriteStep):
        consumes_candidates = False

        def __init__(self, mapping):
            self._m = mapping

        def apply(self, decision):
            decision.claims = list(self._m.get(decision.text, []))

    utc = "All timestamps are stored in UTC."
    local = "Timestamps in the events table use the server's local time."
    mapping = {
        utc: [Claim(subject="timestamps", attribute="timezone", value="UTC", functional=True)],
        local: [Claim(subject="timestamps", attribute="timezone", value="local", functional=True)],
    }
    judge = ClaimValueJudge(llm=FakeLlm(default='{"incompatible": true}'))
    graph = VectorGraph(policy=[_Claims(mapping), ClaimConflictDetector(judge=judge)])
    graph.write(utc, state=state_first)
    graph.write(local, state=state_second)
    return graph


def test_established_incumbent_stays_active_newcomer_is_held():
    # Established fact (direct approval -> active) vs a newcomer that arrives through
    # ingestion (-> proposed). The incumbent keeps its place in the active graph;
    # only the newcomer is held for review. Both are still linked so the pair shows.
    graph = _contradicting_graph("active", "proposed")
    candidates = candidates_from_graph(graph)
    by_text = {c["content"][:14]: c for c in candidates}
    incumbent = by_text["All timestamps"]
    newcomer = by_text["Timestamps in "]
    assert incumbent["state"] == "active"  # incumbent stays in the active graph
    assert newcomer["state"] == "proposed"  # only the newcomer is held
    assert incumbent["contradiction_ids"] and newcomer["contradiction_ids"]


def test_two_newcomers_are_both_held():
    # Two facts ingested in the same batch (both proposed) clash -> both held; neither
    # enters the active graph. (Write order doesn't make the earlier one "established".)
    graph = _contradicting_graph("proposed", "proposed")
    candidates = candidates_from_graph(graph)
    assert {c["state"] for c in candidates} == {"proposed"}
    assert all(c["contradiction_ids"] for c in candidates)
