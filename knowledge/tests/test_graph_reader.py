"""Tests for the GraphReader variants (WholeFileReader, RetrievingReader)."""

import pytest

from knowledge.graph_reader.grapher_reader_variants.retrieving_reader import (
    RetrievingReader,
)
from knowledge.graph_reader.grapher_reader_variants.whole_file_reader import (
    WholeFileReader,
    as_claude_tool,
)
from knowledge.knowledge_graph.knowledge_graph_variants.in_memory_graph import (
    InMemoryGraph,
)
from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import VectorGraph
from knowledge.llm.parent_embedder import Embedder


def _graph_with(*lines):
    graph = InMemoryGraph()
    for line in lines:
        graph.write(line)
    return graph


class _StubEmbedder(Embedder):
    """Deterministic, semantically-meaningful-enough embedder for ranking tests.

    Maps a small vocabulary to orthogonal axes, so a fact about "caching" ranks
    high for a caching query and a fact about "xray" ranks at zero — unlike
    FakeEmbedder's hash vectors, which can't discriminate.
    """

    VOCAB = ["caching", "todo", "xray", "ses"]

    def embed(self, texts):
        out = []
        for text in texts:
            low = text.lower()
            vec = [1.0 if word in low else 0.0 for word in self.VOCAB]
            if not any(vec):
                vec = [0.0, 0.0, 0.0, 0.0]  # unrelated -> zero similarity to any query
            out.append(vec)
        return out


def _vector_graph_with(*lines):
    graph = VectorGraph(embedder=_StubEmbedder(), policy=[])  # store verbatim, no dedup/redact
    for line in lines:
        graph.write(line, state="active")  # only active facts are retrievable
    return graph


def test_synthesis_returns_single_whole_graph_request():
    reader = WholeFileReader(_graph_with("x"))
    requests = reader.synthesis("any context")
    assert len(requests) == 1


def test_read_returns_full_graph():
    reader = WholeFileReader(_graph_with("alpha", "beta"))
    content = reader.read()
    assert "alpha" in content and "beta" in content


def test_claude_tool_adapter_returns_contents():
    reader = WholeFileReader(_graph_with("tool-visible"))
    tool = as_claude_tool(reader)
    assert tool["name"] == "read_knowledge"
    assert "tool-visible" in tool["func"]()


# --- RetrievingReader: rank, threshold, cap, guard ---------------------------

_FACTS = (
    "use caching for the data-fetch layer",  # relevant
    "always prefix TODO(MD) on every todo",  # relevant
    "tracing uses xray sampling in prod",  # irrelevant -> score 0
    "email goes through ses templates",  # irrelevant -> score 0
)


def test_retrieving_reader_floor_drops_irrelevant_facts():
    # Floor isolated (rel_ratio=0): irrelevant facts (score 0) fall below the floor.
    reader = RetrievingReader(
        _vector_graph_with(*_FACTS), top_k=10, abs_floor=0.5, rel_ratio=0.0
    )
    out = reader.read("add caching and a todo comment")
    assert "caching" in out and "TODO(MD)" in out  # relevant kept (score ~0.71)
    assert "xray" not in out and "ses" not in out  # below the floor -> dropped


def test_retrieving_reader_top_k_caps_count():
    # Cutoff disabled (floor=0, ratio=0) so only the top_k cap applies.
    reader = RetrievingReader(
        _vector_graph_with(*_FACTS), top_k=1, abs_floor=0.0, rel_ratio=0.0
    )
    out = reader.read("add caching and a todo comment")
    assert len([p for p in out.split("\n\n") if p]) == 1  # only the single best hit


def test_retrieving_reader_requires_searchable_graph():
    with pytest.raises(TypeError):
        RetrievingReader(_graph_with("x"))  # InMemoryGraph is not searchable
