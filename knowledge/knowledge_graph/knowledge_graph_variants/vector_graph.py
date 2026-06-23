"""Vector-backed knowledge store with a composable write-policy pipeline.

The credible-baseline replacement for ``InMemoryGraph``: facts carry metadata
and an embedding, ``write`` runs the redact -> dedup -> conflict-flag pipeline
before persisting, and ``search`` does cosine similarity retrieval (satisfying
``SearchableGraph`` for the retrieving reader).

Storage is in-process for now (a list of facts) — fine for the per-case eval
lifecycle. The persistence backend (sqlite / sqlite-vec / LanceDB) is a
swappable internal detail behind this same class; nothing above it changes when
it lands. ``write(content)`` only receives text (the frozen contract), so facts
are stored with default metadata; provenance/scope flow in a later pass.
"""

from __future__ import annotations

import math
import uuid

from knowledge.knowledge_graph.knowledge_graph_def import Contradiction, Fact, SearchHit
from knowledge.knowledge_graph.parent_searchable_graph import SearchableGraph
from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision
from knowledge.knowledge_graph.write_policy.write_step_variants import (
    ConflictFlagger,
    Deduper,
    Redactor,
)
from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder
from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm
from knowledge.llm.parent_embedder import Embedder
from knowledge.llm.parent_llm import Llm


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def default_write_policy(llm: Llm | None = None) -> list[WriteStep]:
    """The baseline pipeline: redact, then dedup, then conflict-flag.

    ``llm`` powers the contradiction check; defaults to OpenRouter. Detection is
    best-effort — ``ConflictFlagger`` skips silently if the LLM is unavailable
    (e.g. no API key offline), so this is safe to leave on by default.
    """
    return [Redactor(), Deduper(), ConflictFlagger(llm=llm or OpenRouterLlm())]


class VectorGraph(SearchableGraph):
    """An embedded vector store of facts with write-time policy and search."""

    def __init__(
        self,
        embedder: Embedder | None = None,
        policy: list[WriteStep] | None = None,
    ) -> None:
        # Deterministic offline default; inject OpenRouterEmbedder for real runs.
        self.embedder = embedder or FakeEmbedder()
        self.policy = policy if policy is not None else default_write_policy()
        self._facts: list[Fact] = []

    # --- KnowledgeGraph contract -------------------------------------------
    def read(self, context: str | None = None) -> str:
        """Return all fact texts concatenated (context ignored; reader filters)."""
        return "\n\n".join(f.text for f in self._facts)

    def write(self, content: str, *, state: str = "proposed") -> None:
        """Run the write-policy pipeline over ``content``, then persist.

        ``state`` ("active" for a direct user approval, "proposed" for a passive
        system add) is the lifecycle state a freshly-added fact lands in.
        """
        content = content.strip()
        if not content:
            return
        decision = WriteDecision(text=content, state="active" if state == "active" else "proposed")
        for step in self.policy:
            step.apply(decision, self)
        if decision.dropped:
            return
        if decision.action == "update" and decision.update_target_id:
            self._merge(decision)
            return
        if decision.action == "overwrite" and decision.update_target_id:
            self._overwrite(decision)
            return
        self._add(decision)

    # --- SearchableGraph contract ------------------------------------------
    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        filters: dict | None = None,
        scope: str | None = None,
    ) -> list[SearchHit]:
        candidates = [
            f
            for f in self._facts
            if (scope is None or f.scope == scope)
            and all(getattr(f, k, None) == v for k, v in (filters or {}).items())
        ]
        if not candidates:
            return []
        qvec = self.embedder.embed_one(query)
        hits = [
            SearchHit(fact=f, score=_cosine(qvec, f.embedding))
            for f in candidates
            if f.embedding is not None
        ]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    # --- contradiction review surface --------------------------------------
    def contradictions(self) -> list[Contradiction]:
        """Facts the write-policy flagged as contradicting an existing fact.

        This is the elevation surface: pairs ready to hand to a reviewer (e.g.
        the dashboard's Contradictions tab) for keep/reject resolution.
        """
        by_id = {f.id: f for f in self._facts}
        pairs: list[Contradiction] = []
        for fact in self._facts:
            for flag in fact.flags:
                if flag.startswith("contradiction:"):
                    other = by_id.get(flag.split(":", 1)[1])
                    if other is not None:
                        pairs.append(Contradiction(flagged=fact, conflicting=other))
        return pairs

    # --- StoreView (used by write steps) -----------------------------------
    def most_similar(self, text: str, k: int = 5) -> list[SearchHit]:
        return self.search(text, top_k=k)

    @property
    def facts(self) -> list[Fact]:
        """Stored facts (read-only view for export/adapters)."""
        return list(self._facts)

    # --- internals ----------------------------------------------------------
    def _add(self, decision: WriteDecision) -> None:
        self._facts.append(
            Fact(
                id=uuid.uuid4().hex,
                text=decision.text,
                state=decision.state,
                embedding=self.embedder.embed_one(decision.text),
                flags=list(decision.flags),
            )
        )

    def _merge(self, decision: WriteDecision) -> None:
        for fact in self._facts:
            if fact.id == decision.update_target_id:
                fact.observation_count += 1
                fact.confidence = min(1.0, fact.confidence + 0.05)
                fact.flags.extend(decision.flags)
                return

    def _overwrite(self, decision: WriteDecision) -> None:
        """Forced upsert (approved path): the new fact replaces the nearest
        conflicting one in place, and every other contradiction decays — so no
        contradictory pair lingers and the newest approved truth is the survivor.
        """
        targets = {decision.update_target_id, *decision.supersede_ids}
        for fact in self._facts:
            if fact.id == decision.update_target_id:
                fact.text = decision.text
                fact.state = decision.state
                fact.observation_count += 1
                fact.confidence = 1.0
                fact.embedding = self.embedder.embed_one(decision.text)
            elif fact.id in targets:
                fact.state = "decayed"
