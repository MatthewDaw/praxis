"""Abstract ingestor contract.

``ingest`` is *concrete* — it runs the same way for every variant: synthesize a
list of :class:`Insight` objects from the raw input, then write each into the
graph. The variable, model-specific step is the abstract :meth:`Ingestor.synthesis`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel

from praxis.knowledge.knowledge_graph.parent_knowledge_graph import KnowledgeGraph


class Insight(BaseModel):
    """A single distilled unit of knowledge.

    One field for now; expand as the distillation gets richer (source,
    confidence, tags, ...).
    """

    raw_text: str


class Ingestor(ABC):
    """Distills raw input into the knowledge graph."""

    def __init__(self, graph: KnowledgeGraph) -> None:
        self.graph = graph

    @abstractmethod
    def synthesis(self, raw_input: str) -> list[Insight]:
        """Transform raw input into structured insights. Variant-defined."""

    def ingest(self, raw_input: str) -> str:
        """Synthesize insights from ``raw_input`` and write each to the graph.

        Concrete and final for the MVP — runs every time. Returns the graph
        content after ingestion for inspection/testing.
        """
        for insight in self.synthesis(raw_input):
            self.graph.write(insight.raw_text)
        return self.graph.read()
