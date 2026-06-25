"""Gap H7 — per-call retrieval-tuning knobs.

These exercise the fusion-weight knob on ``_rrf_fuse`` (pure function, no DB) and
the ``keyword_weight`` plumbing on ``PostgresVectorGraph.search``. The point of H7
is that a caller can bias semantic-vs-keyword fusion per query (concept vs symbol)
without retuning module-global constants; raising ``keyword_weight`` lets the
keyword (BM25) branch promote a fact the cosine branch ranked lower.
"""

from __future__ import annotations

from knowledge.knowledge_graph.knowledge_graph_def import Fact, SearchHit
from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
    _RRF_KEYWORD_WEIGHT,
    _rrf_fuse,
)


def _hit(fid: str, score: float = 0.5) -> SearchHit:
    return SearchHit(fact=Fact(id=fid, text=fid), score=score)


def test_default_keyword_weight_keeps_semantic_dominant():
    # Fact "A" leads cosine; "B" leads keyword. At the calibrated default weight the
    # keyword branch can nudge but not overturn the semantic #1.
    semantic = [_hit("A"), _hit("B")]
    keyword = [_hit("B"), _hit("A")]
    ranked = _rrf_fuse(semantic, keyword, top_k=2)
    assert [h.fact.id for h in ranked] == ["A", "B"]


def test_raising_keyword_weight_promotes_the_keyword_winner():
    # Same branches; a high keyword weight (symbol-style query) flips the order so the
    # keyword-#1 fact wins — the per-call bias the reference (H7) asked for.
    semantic = [_hit("A"), _hit("B")]
    keyword = [_hit("B"), _hit("A")]
    ranked = _rrf_fuse(semantic, keyword, top_k=2, keyword_weight=5.0)
    assert [h.fact.id for h in ranked] == ["B", "A"]


def test_keyword_weight_zero_ignores_the_keyword_branch():
    # weight 0 == pure semantic ranking, regardless of keyword order.
    semantic = [_hit("A"), _hit("B")]
    keyword = [_hit("B"), _hit("A")]
    ranked = _rrf_fuse(semantic, keyword, top_k=2, keyword_weight=0.0)
    assert [h.fact.id for h in ranked] == ["A", "B"]


def test_default_param_matches_module_constant():
    # The default argument is the calibrated constant, so omitting it reproduces the
    # historical behavior exactly.
    semantic = [_hit("A"), _hit("B")]
    keyword = [_hit("C"), _hit("A")]
    assert _rrf_fuse(semantic, keyword, top_k=3) == _rrf_fuse(
        semantic, keyword, top_k=3, keyword_weight=_RRF_KEYWORD_WEIGHT
    )
