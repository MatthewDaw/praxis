"""Abstract knowledge-graph contract.

The ``KnowledgeGraph`` is the storage primitive both the ingestor and the graph
reader depend on. The "graph" is whatever a concrete variant chooses to persist;
for the MVP that is a single ``CLAUDE.md`` file (see
``knowledge_graph_variants.claude_md_graph``).

Freeze this contract: variants may change freely, callers must not.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class KnowledgeGraph(ABC):
    """The knowledge store. The 'graph' is whatever a variant persists."""

    @abstractmethod
    def read(self, context: str | None = None) -> str:
        """Return knowledge content.

        ``context`` (optional, default ``None``) hints what to retrieve. A
        variant is free to ignore it and return everything — the MVP
        ``ClaudeMdGraph`` does exactly that.
        """

    @abstractmethod
    def write(self, content: str) -> None:
        """Persist ``content`` into the store.

        Semantics are integrate/append, not replace: the ingestor calls this
        once per distilled insight, so a replacing implementation would clobber
        all but the last write.
        """
