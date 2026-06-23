"""Relevance-ranking reader: search the graph, return only the facts that matter.

Where :class:`WholeFileReader` dumps the entire graph unranked, this reader turns
the context into a similarity search and applies a model-robust cutoff. It needs a
:class:`SearchableGraph` (the vector store) and a *real* embedder to make the
ranking meaningful — ``FakeEmbedder``'s hash vectors cannot discriminate.

The cutoff is **floor → relative → cap**, three global constants that are the
reader's system contract:

1. ``abs_floor`` (existence): drop anything below it. This is the negative-control
   guard — a no-good-match query (every hit below the floor) returns nothing, so
   nothing irrelevant is injected downstream. Coarse and only mildly model-tied.
2. ``rel_ratio`` (shape): keep hits within that fraction of the best score. This is
   the model-robust precision knob — it adapts per query and per model, so an
   embedder swap needs no precise separating value retuned.
3. ``top_k`` (volume): a backstop cap. The search pool is ``top_k``, so the cap is
   enforced by retrieval and floor/relative only trim within it.

Set ``abs_floor=0`` or ``rel_ratio=0`` to disable a mechanism (used by tests to
isolate the other).

Default calibration (``abs_floor=0.30``, ``rel_ratio=0.75``, ``top_k=8``) is tuned
against ``openai/text-embedding-3-small``: on the reader graph the relevant facts
score ~0.45-0.52 and the distractors sit <=0.27, while a no-relevant-fact query
tops out ~0.2. The floor (0.30) sits in the unrelated band — coarse, not on the
separating line — and the ratio (0.75) drops anything much weaker than the best.
Validated end-to-end: ``lost_in_middle_reader`` (relative cutoff isolated) and the
live ``lost_in_middle`` (full production triple) both pass. Recompute the floor on
an embedding-model change; the relative ratio is model-robust and should carry over.
"""

from __future__ import annotations

from knowledge.graph_reader.graph_reader_def import ReadRequest
from knowledge.graph_reader.parent_graph_reader import GraphReader
from knowledge.knowledge_graph.knowledge_graph_def import SearchHit
from knowledge.knowledge_graph.parent_searchable_graph import SearchableGraph


class RetrievingReader(GraphReader):
    """Returns only the facts relevant to the context, ranked by similarity."""

    def __init__(
        self,
        graph: SearchableGraph,
        *,
        top_k: int = 8,
        abs_floor: float = 0.30,
        rel_ratio: float = 0.75,
    ) -> None:
        if not isinstance(graph, SearchableGraph):
            raise TypeError(
                f"RetrievingReader needs a SearchableGraph, got {type(graph).__name__}"
            )
        super().__init__(graph)
        self.top_k = top_k
        self.abs_floor = abs_floor
        self.rel_ratio = rel_ratio

    def synthesis(self, context: str | None = None) -> list[ReadRequest]:
        return [ReadRequest(query=context or "", top_k=self.top_k)]

    def _cutoff(self, hits: list[SearchHit]) -> list[SearchHit]:
        """Apply floor → relative → cap (operates on existing scores only)."""
        hits = [h for h in hits if h.score >= self.abs_floor]  # 1. existence
        if hits and self.rel_ratio > 0:
            top = max(h.score for h in hits)
            hits = [h for h in hits if h.score >= self.rel_ratio * top]  # 2. shape
        return hits[: self.top_k]  # 3. volume cap

    def read(self, context: str | None = None) -> str:
        """Search per request, apply the cutoff, concatenate the survivors.

        Overrides the base ``read`` (which dumps the whole graph) so retrieval
        actually ranks-and-trims instead of dumping.
        """
        parts: list[str] = []
        for req in self.synthesis(context):
            hits = self.graph.search(
                req.query,
                top_k=req.top_k,
                filters=req.filters or None,
                scope=req.scope,
            )
            parts.extend(h.fact.text for h in self._cutoff(hits))
        return "\n\n".join(parts)
