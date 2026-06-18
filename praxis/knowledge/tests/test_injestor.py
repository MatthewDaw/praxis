"""Tests for the PromptIngestor variant of Ingestor."""

from praxis.knowledge.injestion.injestor_variants.prompt_injestor import PromptIngestor
from praxis.knowledge.knowledge_graph.knowledge_graph_variants.claude_md_graph import (
    ClaudeMdGraph,
)


def test_ingest_without_llm_writes_raw_input(tmp_path):
    graph = ClaudeMdGraph(tmp_path / "CLAUDE.md")
    ingestor = PromptIngestor(graph)  # no llm -> passthrough
    out = ingestor.ingest("prefer pathlib")
    assert "prefer pathlib" in out
    assert "prefer pathlib" in graph.read()


def test_synthesis_splits_llm_response_into_insights(tmp_path):
    graph = ClaudeMdGraph(tmp_path / "CLAUDE.md")
    # Fake LLM returns two lines -> two insights.
    ingestor = PromptIngestor(graph, llm=lambda prompt: "insight A\n\ninsight B\n")
    insights = ingestor.synthesis("raw")
    assert [i.raw_text for i in insights] == ["insight A", "insight B"]


def test_ingest_loops_write_over_all_insights(tmp_path):
    graph = ClaudeMdGraph(tmp_path / "CLAUDE.md")
    ingestor = PromptIngestor(graph, llm=lambda prompt: "one\ntwo\nthree")
    ingestor.ingest("raw")
    content = graph.read()
    assert "one" in content and "two" in content and "three" in content
