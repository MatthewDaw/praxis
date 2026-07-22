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
    def synthesis(
        self, raw_input: str, *, source: str | None = None, atomic: bool = False
    ) -> list[Insight]:
        """Transform raw input into structured insights. Variant-defined.

        ``source`` is the document's origin/identifier (e.g. a citation or form
        section). Variants that distill with an LLM may use it as context to
        resolve in-document references; deterministic variants can ignore it.

        ``atomic`` requests that the input be kept WHOLE as a single insight —
        no distillation/segmentation. Callers use it for already-shaped facts
        (e.g. a settled requirement admitted via ``add_insight``) that must stay
        a single fact rather than fragmenting one-per-sentence. Variants that
        cannot honour it may ignore it, but should keep the input atomic when set.
        """

    def ingest(
        self,
        raw_input: str,
        *,
        state: str = "proposed",
        source: str | None = None,
        scope: str | None = None,
        category: str | None = None,
        meta: dict | None = None,
        atomic: bool = False,
    ) -> str:
        """Synthesize insights from ``raw_input`` and write each to the graph.

        Concrete and final for the MVP — runs every time. Returns the graph
        content after ingestion for inspection/testing.

        ``state`` is the lifecycle state distilled facts land in. It defaults to
        "proposed": ingestion is a *passive* add (the system distilling raw
        input), so its output is staged, not endorsed. A caller enacting a direct
        user approval passes ``state="active"``.

        ``source`` is threaded both into ``synthesis`` (as distillation context)
        and onto each written fact's provenance.

        ``scope``/``category``/``meta`` are writer-supplied metadata (gap H12)
        stamped onto every fact this call writes. Precedence is **writer wins,
        ingestion-derived fills unset**: a writer value overrides whatever
        ``synthesis`` put on the insight; when the writer leaves a field unset the
        per-insight value (if any) carries through, and only then does the store's
        ingestion-derived default apply. ``meta`` is writer-only (insights carry
        no meta of their own).

        ``atomic`` is threaded to ``synthesis`` to keep the input WHOLE (one fact,
        no segmentation) — used by the shaped-fact write lane (``add_insight``) so a
        pre-atomic insight never fragments. Reconciliation downstream (dedup,
        contradiction surfacing) still runs in ``graph.write``.
        """
        insights = self.synthesis(raw_input, source=source, atomic=atomic)
        for insight in insights:
            # Only thread optional kwargs through when they carry signal: not every
            # graph implementation (in-memory/test doubles) accepts them. ``source``
            # is the persistent store's fact provenance; ``tabular`` flags a
            # table-derived write so the deduper's slot-guard engages downstream.
            kwargs: dict = {"state": state}
            for key, wval, ival in (
                ("source", source, insight.source),
                ("scope", scope, insight.scope),
                ("category", category, insight.category),
            ):
                eff = wval if wval is not None else ival
                if eff is not None:
                    kwargs[key] = eff
            if meta:
                kwargs["meta"] = meta
            if insight.tabular:
                kwargs["tabular"] = True
            self.graph.write(insight.raw_text, **kwargs)
        return self.graph.read()
