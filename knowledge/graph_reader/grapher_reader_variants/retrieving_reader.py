"""Relevance-ranking reader: search the graph, return only the facts that matter.

Where :class:`WholeFileReader` dumps the entire graph unranked, this reader turns
the context into a similarity search and returns only the facts that clear a
relevance threshold (then capped at ``top_k``). It needs a
:class:`SearchableGraph` (the vector store), and a *real* embedder to make the
ranking meaningful — ``FakeEmbedder``'s hash vectors cannot discriminate.

The ``min_score`` cutoff, not ``top_k`` alone, is what makes "drop the irrelevant
facts" robust: top-k by itself always returns ``k`` facts regardless of how
relevant they are, so exclusion would hinge on rank position. Filtering by score
first means unrelated facts fall out on their own.
"""

from __future__ import annotations

from knowledge.graph_reader.graph_reader_def import ReadRequest
from knowledge.graph_reader.parent_graph_reader import GraphReader
from knowledge.knowledge_graph.parent_searchable_graph import SearchableGraph


class RetrievingReader(GraphReader):
    """Returns only the facts relevant to the context, ranked by similarity."""

    def __init__(
        self, graph: SearchableGraph, *, top_k: int = 8, min_score: float = 0.0
    ) -> None:
        if not isinstance(graph, SearchableGraph):
            raise TypeError(
                f"RetrievingReader needs a SearchableGraph, got {type(graph).__name__}"
            )
        super().__init__(graph)
        self.top_k = top_k
        self.min_score = min_score

    def synthesis(self, context: str | None = None) -> list[ReadRequest]:
        return [ReadRequest(query=context or "", top_k=self.top_k)]

    def read(self, context: str | None = None) -> str:
        """Search per request, drop hits below ``min_score``, concatenate the rest.

        Overrides the base ``read`` (which calls ``graph.read`` and returns the
        whole graph) so retrieval actually ranks instead of dumping.
        """
        parts: list[str] = []
        for req in self.synthesis(context):
            hits = self.graph.search(
                req.query,
                top_k=req.top_k,
                filters=req.filters or None,
                scope=req.scope,
            )
            parts.extend(h.fact.text for h in hits if h.score >= self.min_score)
        return "\n\n".join(parts)
