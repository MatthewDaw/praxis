"""Abstract ingestor contract.

``ingest`` is *concrete* — it runs the same way for every variant: synthesize a
list of :class:`Insight` objects from the raw input, then write each into the
graph. The variable, model-specific step is the abstract :meth:`Ingestor.synthesis`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from knowledge.injestion.injestion_def import Insight
from knowledge.knowledge_graph.parent_knowledge_graph import KnowledgeGraph


class Ingestor(ABC):
    """Distills raw input into the knowledge graph."""

    def __init__(self, graph: KnowledgeGraph) -> None:
        self.graph = graph

    @abstractmethod
    def synthesis(self, raw_input: str) -> list[Insight]:
        """Transform raw input into structured insights. Variant-defined."""

    def ingest(self, raw_input: str, *, state: str = "proposed") -> str:
        """Synthesize insights from ``raw_input`` and write each to the graph.

        Concrete and final for the MVP — runs every time. Returns the graph
        content after ingestion for inspection/testing.

        ``state`` is the lifecycle state distilled facts land in. It defaults to
        "proposed": ingestion is a *passive* add (the system distilling raw
        input), so its output is staged, not endorsed. A caller enacting a direct
        user approval passes ``state="active"``.
        """
        insights = self.synthesis(raw_input)
        for insight in insights:
            self.graph.write(insight.raw_text, state=state)
        return self.graph.read()
