"""Wire a concrete knowledge trio (graph + ingestor + reader).

Lives in its own module so both the entrypoint (``knowledge.run``) and the
harness (``knowledge.evals.run``) can import it without a circular dependency.

``build_trio`` selects the substrate:
- ``in_memory`` (default) — the deterministic stub trio (in-memory string graph).
- ``vector``  — the baseline trio (vector store + write-policy pipeline). Uses
  the deterministic FakeEmbedder by default so it stays offline/CI-safe; inject
  a real embedder/graph for production runs.
"""

from __future__ import annotations

from knowledge.graph_reader.grapher_reader_variants.retrieving_reader import (
    RetrievingReader,
)
from knowledge.graph_reader.grapher_reader_variants.whole_file_reader import (
    WholeFileReader,
)
from knowledge.injestion.injestor_variants.prompt_injestor import PromptIngestor
from knowledge.knowledge_graph.knowledge_graph_variants.in_memory_graph import (
    InMemoryGraph,
)
from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import VectorGraph
from knowledge.knowledge_graph.parent_knowledge_graph import KnowledgeGraph
from knowledge.knowledge_graph.parent_searchable_graph import SearchableGraph
from knowledge.llm.parent_embedder import Embedder


def _graph_for(substrate: str, embedder: Embedder | None = None) -> KnowledgeGraph:
    if substrate == "vector":
        return VectorGraph(embedder=embedder)  # None => VectorGraph's FakeEmbedder default
    if substrate == "in_memory":
        return InMemoryGraph.create()
    if substrate == "postgres":
        # The persistent store needs an explicit tenant; the backend injects a
        # per-request instance via ``build_trio(graph=…)`` instead of going
        # through ``substrate``. For standalone use (e.g. evals), supply tenancy
        # via PRAXIS_ORG_ID / PRAXIS_USER_ID.
        import os

        from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
            PostgresVectorGraph,
        )
        from knowledge.serve import db

        org_id = os.environ.get("PRAXIS_ORG_ID")
        user_id = os.environ.get("PRAXIS_USER_ID")
        if not org_id or not user_id:
            raise ValueError(
                "substrate 'postgres' needs tenancy: set PRAXIS_ORG_ID and "
                "PRAXIS_USER_ID, or inject a PostgresVectorGraph via build_trio(graph=…)."
            )
        return PostgresVectorGraph(db.connect(), org_id, user_id)
    raise ValueError(f"unknown substrate: {substrate!r}")


def build_trio(
    substrate: str = "in_memory",
    graph: KnowledgeGraph | None = None,
    llm=None,
    reader: str = "whole_file",
    embedder: Embedder | None = None,
    reader_top_k: int | None = None,
    reader_abs_floor: float | None = None,
    reader_rel_ratio: float | None = None,
):
    """Return a wired ``(graph, ingestor, reader)`` for the chosen substrate.

    Pass ``graph`` to wire a specific store instance (overrides ``substrate``).
    ``reader`` picks the retrieval strategy: ``"whole_file"`` dumps the graph,
    ``"retrieving"`` ranks via the store's ``search`` (needs a ``SearchableGraph``).
    ``embedder`` is injected into the vector store; ``reader_top_k`` /
    ``reader_abs_floor`` / ``reader_rel_ratio`` override the retrieving reader's
    cutoff (None => defaults; 0 disables a mechanism for test isolation).
    """
    graph = graph or _graph_for(substrate, embedder=embedder)
    ingestor = PromptIngestor(graph, llm=llm)
    if reader == "retrieving":
        if not isinstance(graph, SearchableGraph):
            raise ValueError(
                f"reader 'retrieving' needs a searchable substrate (vector/postgres), "
                f"got {type(graph).__name__}"
            )
        kwargs = {}
        if reader_top_k is not None:
            kwargs["top_k"] = reader_top_k
        if reader_abs_floor is not None:
            kwargs["abs_floor"] = reader_abs_floor
        if reader_rel_ratio is not None:
            kwargs["rel_ratio"] = reader_rel_ratio
        reader_obj: object = RetrievingReader(graph, **kwargs)
    else:
        reader_obj = WholeFileReader(graph)
    return graph, ingestor, reader_obj
