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
from typing import TYPE_CHECKING

from knowledge.knowledge_graph.knowledge_graph_def import SearchHit
from knowledge.knowledge_graph.parent_knowledge_graph import KnowledgeGraph

if TYPE_CHECKING:
    from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision


class SearchableGraph(KnowledgeGraph):
    """A knowledge graph that supports similarity/keyword retrieval."""

    def _run_policy(self, decision: "WriteDecision") -> None:
        """Run the ordered write-policy steps over ``decision`` in place.

        Each recall set is filled lazily the first time a step that consumes it runs:
        the shared candidate pass (embeds the incoming text once), the wider semantic
        pass, and the claim-slot pass (after ClaimExtractor). A pass runs at most once.
        Shared by every store's write/decide entry point; the subclasses supply the
        ``policy`` and the ``_recall*`` hooks.
        """
        claim_recalled = False
        semantic_recalled = False
        for step in self.policy:
            if step.consumes_candidates and decision.embedding is None:
                self._recall(decision)  # embed once + one shared candidate pass
            if step.consumes_semantic_candidates and not semantic_recalled:
                self._recall_semantic(decision)  # wider recall for the semantic pass
                semantic_recalled = True
            if step.consumes_claim_candidates and not claim_recalled:
                self._recall_claims(decision)  # slot recall, after ClaimExtractor ran
                claim_recalled = True
            step.apply(decision)

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        filters: dict | None = None,
        scope: str | None = None,
        state: str | None = "active",
        hybrid: bool = False,
        keyword_weight: float | None = None,
        exclude_categories: list[str] | None = None,
    ) -> list[SearchHit]:
        """Return the most relevant stored facts for ``query`` (best first).

        ``state`` gates which lifecycle state is retrievable and defaults to
        ``"active"``: only endorsed facts are surfaced, so ``proposed`` (staged)
        and ``rejected`` (retired) facts stay out of retrieval. Pass ``state=None``
        to search across all states — used by the write-policy's dedup/conflict
        lookup (``most_similar``), which must see pending facts.
        """
