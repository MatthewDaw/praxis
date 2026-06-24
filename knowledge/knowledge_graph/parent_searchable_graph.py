"""Search capability, layered onto the frozen ``KnowledgeGraph`` contract.

The base contract is deliberately read/write only. A retrieving reader needs
similarity search, so rather than widen the frozen base (which every variant —
including the in-memory stub — would then have to honor), search is added as a
focused capability interface: ``SearchableGraph`` is-a ``KnowledgeGraph`` that
*also* searches. Only stores that can search implement it; the reader depends on
this interface, not on a concrete store.
"""

from __future__ import annotations

from abc import abstractmethod

from knowledge.knowledge_graph.knowledge_graph_def import SearchHit
from knowledge.knowledge_graph.parent_knowledge_graph import KnowledgeGraph


class SearchableGraph(KnowledgeGraph):
    """A knowledge graph that supports similarity/keyword retrieval."""

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        filters: dict | None = None,
        scope: str | None = None,
        state: str | None = "active",
    ) -> list[SearchHit]:
        """Return the most relevant stored facts for ``query`` (best first).

        ``state`` gates which lifecycle state is retrievable and defaults to
        ``"active"``: only endorsed facts are surfaced, so ``proposed`` (staged)
        and ``rejected`` (retired) facts stay out of retrieval. Pass ``state=None``
        to search across all states — used by the write-policy's dedup/conflict
        lookup (``most_similar``), which must see pending facts.
        """
