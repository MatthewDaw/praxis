"""Abstract knowledge-graph contract.

The ``KnowledgeGraph`` is the storage primitive both the ingestor and the graph
reader depend on. The "graph" is whatever a concrete variant holds; for the MVP
that is an in-memory string (see ``knowledge_graph_variants.in_memory_graph``) —
no file, nothing on disk. A variant owns its own initialization/provisioning.

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
        ``InMemoryGraph`` does exactly that.
        """

    @abstractmethod
    def write(self, content: str, *, state: str = "proposed") -> None:
        """Persist ``content`` into the store.

        Semantics are integrate/append, not replace: the ingestor calls this
        once per distilled insight, so a replacing implementation would clobber
        all but the last write.

        ``state`` is the lifecycle state the new fact lands in: "active" when the
        caller is enacting a direct user approval, "proposed" (the default) when
        the system is adding knowledge passively. A variant that doesn't track
        state may ignore it.
        """
