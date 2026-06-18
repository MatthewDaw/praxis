"""Tests for the WholeFileReader variant of GraphReader."""

from praxis.knowledge.graph_reader.grapher_reader_variants.whole_file_reader import (
    WholeFileReader,
    as_claude_tool,
)
from praxis.knowledge.knowledge_graph.knowledge_graph_variants.claude_md_graph import (
    ClaudeMdGraph,
)


def _graph_with(tmp_path, *lines):
    graph = ClaudeMdGraph(tmp_path / "CLAUDE.md")
    for line in lines:
        graph.write(line)
    return graph


def test_synthesis_returns_single_whole_graph_request(tmp_path):
    reader = WholeFileReader(_graph_with(tmp_path, "x"))
    requests = reader.synthesis("any context")
    assert len(requests) == 1


def test_read_returns_full_graph(tmp_path):
    reader = WholeFileReader(_graph_with(tmp_path, "alpha", "beta"))
    content = reader.read()
    assert "alpha" in content and "beta" in content


def test_claude_tool_adapter_returns_contents(tmp_path):
    reader = WholeFileReader(_graph_with(tmp_path, "tool-visible"))
    tool = as_claude_tool(reader)
    assert tool["name"] == "read_knowledge"
    assert "tool-visible" in tool["func"]()
