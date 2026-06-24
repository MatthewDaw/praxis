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
    ConflictJudge,
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
    return [Redactor(), Deduper(), ConflictFlagger(judge=ConflictJudge(llm=llm or OpenRouterLlm()))]


class VectorGraph(SearchableGraph):
    """An embedded vector store of facts with write-time policy and search."""

    def __init__(
        self,
        embedder: Embedder | None = None,
        policy: list[WriteStep] | None = None,
        *,
        recall_floor: float = 0.45,
        recall_k: int = 5,
        tag_recall_k: int = 5,
    ) -> None:
        # Deterministic offline default; inject OpenRouterEmbedder for real runs.
        self.embedder = embedder or FakeEmbedder()
        self.policy = policy if policy is not None else default_write_policy()
        # One shared recall gate for both judges (loose, high-recall): the single
        # per-write candidate pass keeps facts scoring >= recall_floor (top recall_k).
        self.recall_floor = recall_floor
        self.recall_k = recall_k
        # Tier-B (gated): bound on the same-tag candidates added for the conflict path.
        self.tag_recall_k = tag_recall_k
        self._facts: list[Fact] = []

    # --- KnowledgeGraph contract -------------------------------------------
    def read(self, context: str | None = None) -> str:
        """Return the ``active`` fact texts concatenated (context ignored).

        Only ``active`` facts are retrievable: ``proposed`` (staged) and
        ``rejected`` (retired) facts are excluded from what the agent reads,
        matching ``search``'s gating.
        """
        return "\n\n".join(f.text for f in self._facts if f.state == "active")

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
            if step.consumes_candidates and decision.embedding is None:
                self._recall(decision)  # embed once + one shared candidate pass
            step.apply(decision)
        if decision.dropped:
            return
        if decision.embedding is None:
            # No candidate-consuming step ran (e.g. a redact-only policy); still
            # embed once for persistence.
            decision.embedding = self.embedder.embed_one(decision.text)
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
        top_k: int | None = 10,
        filters: dict | None = None,
        scope: str | None = None,
        state: str | None = "active",
    ) -> list[SearchHit]:
        candidates = [
            f
            for f in self._facts
            if (scope is None or f.scope == scope)
            and (state is None or f.state == state)
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

    # --- shared per-write recall (feeds the write steps) -------------------
    def _recall(self, decision: WriteDecision) -> None:
        """Embed the incoming text once and run the single candidate pass.

        Fills ``decision.embedding`` and ``decision.candidates`` (existing facts
        scoring >= ``recall_floor``, best first, capped at ``recall_k``). Dedup
        and conflict both see pending facts, so this searches all states. An empty
        graph embeds once and issues no candidate search.
        """
        decision.embedding = self.embedder.embed_one(decision.text)
        if not self._facts:
            return
        hits = [
            SearchHit(fact=f, score=_cosine(decision.embedding, f.embedding))
            for f in self._facts
            if f.embedding is not None
        ]
        hits.sort(key=lambda h: h.score, reverse=True)
        decision.candidates = [h for h in hits if h.score >= self.recall_floor][: self.recall_k]
        # Tier-B (gated): a second recall key for the conflict path only. Existing
        # facts that share an aspect tag with the incoming note — even below the
        # cosine floor — surface as candidates (bounded, best cosine first), unioned
        # into the conflict judge but never the deduper.
        if decision.tags:
            chosen = {h.fact.id for h in decision.candidates}
            tagset = set(decision.tags)
            decision.tag_candidates = [
                h
                for h in hits
                if h.fact.id not in chosen and tagset.intersection(h.fact.tags)
            ][: self.tag_recall_k]

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
                embedding=decision.embedding,  # reuse the vector embedded in _recall
                flags=list(decision.flags),
                tags=list(decision.tags),
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
                fact.embedding = decision.embedding  # reuse the vector from _recall
                fact.tags = list(decision.tags)
            elif fact.id in targets:
                fact.state = "rejected"
