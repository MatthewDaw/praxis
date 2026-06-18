"""Wire a concrete knowledge trio and demo the ingest -> store -> read loop.

Run directly for a quick manual check:

    uv run python -m praxis.knowledge.run
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from praxis.knowledge.graph_reader.grapher_reader_variants.whole_file_reader import (
    WholeFileReader,
    as_claude_tool,
)
from praxis.knowledge.injestion.injestor_variants.prompt_injestor import PromptIngestor
from praxis.knowledge.knowledge_graph.knowledge_graph_variants.claude_md_graph import (
    ClaudeMdGraph,
)


def build_trio(kg_path: str | Path, llm=None):
    """Return a wired ``(graph, ingestor, reader)`` for the given graph path."""
    graph = ClaudeMdGraph(kg_path)
    ingestor = PromptIngestor(graph, llm=llm)
    reader = WholeFileReader(graph)
    return graph, ingestor, reader


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        kg_path = Path(tmp) / "CLAUDE.md"
        graph, ingestor, reader = build_trio(kg_path)

        # Ingest (no LLM -> raw input becomes insights line-by-line).
        ingestor.ingest("Prefer pathlib over os.path for new code.")
        ingestor.ingest("The test suite runs with `uv run pytest`.")

        # Read back via the reader tool surface.
        tool = as_claude_tool(reader)
        print("=== read_knowledge() ===")
        print(tool["func"]())


if __name__ == "__main__":
    main()
