"""Tests for the ClaudeMdGraph variant of KnowledgeGraph."""

from praxis.knowledge.knowledge_graph.knowledge_graph_variants.claude_md_graph import (
    ClaudeMdGraph,
)


def test_write_then_read_roundtrips(tmp_path):
    graph = ClaudeMdGraph(tmp_path / "CLAUDE.md")
    graph.write("first lesson")
    assert "first lesson" in graph.read()


def test_write_appends_does_not_clobber(tmp_path):
    graph = ClaudeMdGraph(tmp_path / "CLAUDE.md")
    graph.write("lesson one")
    graph.write("lesson two")
    content = graph.read()
    assert "lesson one" in content
    assert "lesson two" in content


def test_read_missing_file_is_empty(tmp_path):
    graph = ClaudeMdGraph(tmp_path / "nope.md")
    assert graph.read() == ""


def test_read_ignores_context(tmp_path):
    graph = ClaudeMdGraph(tmp_path / "CLAUDE.md")
    graph.write("everything")
    assert graph.read(context="anything") == graph.read()


def test_empty_write_is_noop(tmp_path):
    graph = ClaudeMdGraph(tmp_path / "CLAUDE.md")
    graph.write("   ")
    assert graph.read() == ""
