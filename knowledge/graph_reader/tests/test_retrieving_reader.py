"""Reader cutoff (floor → relative → cap) — mechanism-isolation + integration.

A stub ``SearchableGraph`` returns preset scored hits so the cutoff is tested on
controlled scores (no embedder/fixtures). Each isolation test neutralizes one
mechanism (``abs_floor=0`` or ``rel_ratio=0``) so a failure points to a specific
mechanism; the integration test exercises the production defaults together.
"""

from __future__ import annotations

from knowledge.graph_reader.grapher_reader_variants.retrieving_reader import (
    RetrievingReader,
)
from knowledge.knowledge_graph.knowledge_graph_def import Fact, SearchHit
from knowledge.knowledge_graph.parent_searchable_graph import SearchableGraph


class _StubGraph(SearchableGraph):
    """Returns preset (text, score) hits, best-first, capped at top_k."""

    def __init__(self, scored: list[tuple[str, float]]) -> None:
        self._hits = [
            SearchHit(fact=Fact(id=str(i), text=t), score=s)
            for i, (t, s) in enumerate(scored)
        ]

    def search(self, query, *, top_k=10, filters=None, scope=None, exclude_categories=None):
        return sorted(self._hits, key=lambda h: h.score, reverse=True)[:top_k]

    def read(self, context=None):  # KnowledgeGraph contract (unused here)
        return ""

    def write(self, content):  # KnowledgeGraph contract (unused here)
        return None


def _texts(out: str) -> set[str]:
    return {line for line in out.split("\n\n") if line}


def test_relative_cutoff_alone_drops_irrelevant_present():
    # Floor OFF: the relative cutoff alone must drop the low-scoring distractors.
    graph = _StubGraph(
        [("caching", 0.52), ("todo", 0.45), ("cloudfront", 0.27), ("xray", 0.18), ("ses", 0.06)]
    )
    reader = RetrievingReader(graph, top_k=8, abs_floor=0.0, rel_ratio=0.75)
    out = _texts(reader.read("q"))
    assert out == {"caching", "todo"}  # 0.75 * 0.52 = 0.39 → drops 0.27/0.18/0.06


def test_relative_cutoff_keeps_all_varying_strength_relevant():
    # Floor OFF: weaker-but-relevant facts within the ratio must all survive.
    graph = _StubGraph([("a", 0.60), ("b", 0.55), ("c", 0.50), ("d", 0.48)])
    reader = RetrievingReader(graph, top_k=8, abs_floor=0.0, rel_ratio=0.75)
    out = _texts(reader.read("q"))
    assert out == {"a", "b", "c", "d"}  # 0.75 * 0.60 = 0.45 → all kept


def test_floor_alone_empties_a_no_relevant_query():
    # Relative OFF: the floor alone must empty a query whose best hit is weak.
    graph = _StubGraph([("x", 0.20), ("y", 0.15), ("z", 0.08)])
    reader = RetrievingReader(graph, top_k=8, abs_floor=0.30, rel_ratio=0.0)
    assert reader.read("q") == ""  # nothing clears the existence floor


def test_integration_production_defaults_keep_relevant_drop_irrelevant():
    # Floor + relative + cap together (production defaults).
    graph = _StubGraph(
        [("caching", 0.52), ("todo", 0.45), ("cloudfront", 0.27), ("xray", 0.18)]
    )
    reader = RetrievingReader(graph, top_k=8)  # production defaults (abs_floor=0.30, rel_ratio=0.60)
    out = _texts(reader.read("q"))
    assert out == {"caching", "todo"}


def test_cap_bounds_volume():
    # More near-top relevant facts than top_k → cap to top_k (volume backstop).
    graph = _StubGraph([(f"f{i}", 0.90 - i * 0.01) for i in range(12)])
    reader = RetrievingReader(graph, top_k=3, abs_floor=0.0, rel_ratio=0.75)
    assert len(_texts(reader.read("q"))) == 3


def test_top_k_zero_disables_the_cap():
    # top_k=0 turns the volume cap OFF (search all, keep everything above
    # floor/relative) — mirroring abs_floor=0 / rel_ratio=0 disabling their own
    # mechanisms. 12 facts all within the ratio → all 12 survive, none capped.
    graph = _StubGraph([(f"f{i}", 0.90 - i * 0.01) for i in range(12)])
    reader = RetrievingReader(graph, top_k=0, abs_floor=0.0, rel_ratio=0.75)
    assert len(_texts(reader.read("q"))) == 12  # 0.75*0.90=0.675; weakest 0.79 kept


def test_relative_split_is_model_robust_under_scaling():
    # The same relative structure at a different score scale (a "model swap")
    # yields the same partition with the SAME rel_ratio — no precise value retune.
    structure = [("caching", 1.0), ("todo", 0.87), ("cloudfront", 0.52), ("xray", 0.35)]
    keep = {"caching", "todo"}  # 0.75 * top drops cloudfront/xray at any scale
    for scale in (0.52, 0.30, 0.9):
        graph = _StubGraph([(t, s * scale) for t, s in structure])
        reader = RetrievingReader(graph, top_k=8, abs_floor=0.0, rel_ratio=0.75)
        assert _texts(reader.read("q")) == keep
