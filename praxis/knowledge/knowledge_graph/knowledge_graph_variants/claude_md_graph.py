"""MVP knowledge graph: a single ``CLAUDE.md`` file.

No structure, no index — the "graph" is the file's text. ``read`` ignores its
``context`` hint and returns the whole file; ``write`` appends, so successive
insights accumulate rather than overwrite.
"""

from __future__ import annotations

from pathlib import Path

from praxis.knowledge.knowledge_graph.parent_knowledge_graph import KnowledgeGraph

# Separator between appended blocks, kept human-diffable.
_BLOCK_SEP = "\n\n"


class ClaudeMdGraph(KnowledgeGraph):
    """A ``CLAUDE.md``-backed knowledge graph."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self, context: str | None = None) -> str:
        """Return the full file text (``context`` is ignored for the MVP)."""
        if not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8")

    def write(self, content: str) -> None:
        """Append ``content`` as a new block, creating the file if needed."""
        content = content.strip()
        if not content:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.read()
        if existing.strip():
            new = existing.rstrip() + _BLOCK_SEP + content + "\n"
        else:
            new = content + "\n"
        self.path.write_text(new, encoding="utf-8")
