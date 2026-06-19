"""Abstract graph-reader contract.

Mirror of the ingestor: ``read`` is *concrete*, the abstract step is
:meth:`GraphReader.synthesis`. ``read`` turns arbitrary context into a list of
:class:`ReadRequest` objects, runs each against the graph, and concatenates the
results. The MVP variant returns the whole graph in one request.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from knowledge.graph_reader.graph_reader_def import ReadRequest
from knowledge.knowledge_graph.parent_knowledge_graph import KnowledgeGraph


class GraphReader(ABC):
    """Retrieves knowledge for the agent, given current context."""

    def __init__(self, graph: KnowledgeGraph) -> None:
        self.graph = graph

    @abstractmethod
    def synthesis(self, context: str | None = None) -> list[ReadRequest]:
        """Turn arbitrary context into structured read requests. Variant-defined."""

    def read(self, context: str | None = None) -> str:
        """Synthesize read requests, run each against the graph, concatenate.

        Concrete and final for the MVP — runs every time.
        """
        partitioned_input = self.synthesis(context)
        parts = []
        for req in partitioned_input:
            retrieved_context = self.graph.read(req.query)
            parts.append(retrieved_context)
        return "\n".join(part for part in parts if part)
