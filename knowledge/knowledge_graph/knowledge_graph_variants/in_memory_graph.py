"""MVP knowledge graph: an in-memory string.

The graph is a data object Python holds — no file, no path, nothing on disk and
nothing in the repo. ``read`` returns the whole string (ignoring the context
hint); ``write`` appends, so successive insights accumulate. The graph owns its
own initialization via :meth:`create`.

Later variants (file-, vector-, or graph-DB-backed) implement the same
``KnowledgeGraph`` contract and provision their own storage behind ``create``.
"""

from __future__ import annotations

from knowledge.knowledge_graph.parent_knowledge_graph import KnowledgeGraph

_BLOCK_SEP = "\n\n"


class InMemoryGraph(KnowledgeGraph):
    """A knowledge graph backed by a single in-memory string."""

    def __init__(self, content: str = "") -> None:
        self._content = content

    @classmethod
    def create(cls) -> "InMemoryGraph":
        """Provision a fresh, empty graph. The graph's own initializer."""
        return cls()

    def read(self, context: str | None = None) -> str:
        """Return the full content (``context`` is ignored for the MVP)."""
        return self._content

    def write(self, content: str, *, state: str = "proposed") -> None:
        """Append ``content`` as a new block.

        ``state`` is accepted for contract parity but ignored: this MVP string
        graph stores no per-fact metadata, so it can't track lifecycle state.
        """
        content = content.strip()
        if not content:
            return
        if self._content.strip():
            self._content = self._content.rstrip() + _BLOCK_SEP + content + "\n"
        else:
            self._content = content + "\n"
