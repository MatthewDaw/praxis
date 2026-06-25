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
from knowledge.knowledge_graph.write_policy.write_policy_def import (
    ClaimHit,
    WriteDecision,
    demote_active_contradiction,
)
from knowledge.knowledge_graph.write_policy.write_step_variants import (
    TABULAR_FLAG,
    ClaimConflictDetector,
    ClaimExtractionJudge,
    ClaimExtractor,
    ClaimValueJudge,
    Deduper,
    Redactor,
    SemanticConflictDetector,
    SemanticConflictJudge,
    TemporalSupersessionDetector,
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
    """The baseline pipeline: redact, dedup, extract claims, then detect conflicts.

    The structural contradiction path: ``ClaimExtractor`` decomposes the write into
    (subject, attribute, value) claims and ``ClaimConflictDetector`` flags
    same-functional-slot value clashes. ``llm`` powers extraction and the gray-zone
    value judge; both skip silently when the LLM is unavailable (offline), so this
    is safe to leave on by default.

    ``ClaimExtractor`` runs **before** ``Deduper`` so the deduper's tabular slot-guard
    can read ``decision.claims`` (the functional (subject, attribute) slots) when
    deciding whether two sibling rows are a duplicate, a contradiction, or distinct.
    """
    base = llm or OpenRouterLlm()
    return [
        Redactor(),
        ClaimExtractor(judge=ClaimExtractionJudge(llm=base)),
        Deduper(),
        ClaimConflictDetector(judge=ClaimValueJudge(llm=base)),
        # Second-pass semantic fallback (Graphiti two-stage): catches paraphrase
        # contradictions among cosine-recalled neighbours that share no slot.
        SemanticConflictDetector(judge=SemanticConflictJudge(llm=base)),
        # Reinterpret dated same-slot value changes as supersession, not contradiction.
        TemporalSupersessionDetector(),
    ]


class VectorGraph(SearchableGraph):
    """An embedded vector store of facts with write-time policy and search."""

    def __init__(
        self,
        embedder: Embedder | None = None,
        policy: list[WriteStep] | None = None,
        *,
        recall_floor: float = 0.45,
        recall_k: int = 5,
        semantic_recall_floor: float = 0.30,
        semantic_recall_k: int = 10,
        tag_recall_k: int = 5,
    ) -> None:
        # Deterministic offline default; inject OpenRouterEmbedder for real runs.
        self.embedder = embedder or FakeEmbedder()
        self.policy = policy if policy is not None else default_write_policy()
        # One shared recall gate for both judges (loose, high-recall): the single
        # per-write candidate pass keeps facts scoring >= recall_floor (top recall_k).
        self.recall_floor = recall_floor
        self.recall_k = recall_k
        # Wider, lower-floor recall reserved for the semantic contradiction pass.
        self.semantic_recall_floor = semantic_recall_floor
        self.semantic_recall_k = semantic_recall_k
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

    def write(
        self,
        content: str,
        *,
        state: str = "proposed",
        tabular: bool = False,
        source: str | None = None,
        scope: str | None = None,
        category: str | None = None,
        meta: dict | None = None,
    ) -> WriteDecision | None:
        """Run the write-policy pipeline over ``content``, then persist.

        ``state`` ("active" for a direct user approval, "proposed" for a passive
        system add) is the lifecycle state a freshly-added fact lands in.

        ``source``/``scope``/``category``/``meta`` are writer-supplied metadata
        (gap H12) carried onto the freshly-added fact, mirroring the Postgres store
        so the in-memory baseline round-trips the same fields.

        ``tabular`` marks a write distilled from detected tabular input: it stamps
        ``TABULAR_FLAG`` on the decision so the Deduper's slot-guard engages (sibling
        rows must not be silently merged — loss point B).

        Returns the enacted ``WriteDecision`` so callers can observe the per-write
        outcome (``action`` add/update/overwrite, ``dropped``, ``update_target_id``)
        without diffing ``facts`` before/after — an additive change; existing
        callers that ignore the return value are unaffected. Returns ``None`` only
        when nothing was written (empty content), so a ``None`` return is itself a
        "no fact produced" signal. Empty/whitespace input is dropped, not stored.
        """
        content = content.strip()
        if not content:
            return None
        decision = WriteDecision(text=content, state="active" if state == "active" else "proposed")
        if tabular:
            decision.flags.append(TABULAR_FLAG)
        # H12: stash writer metadata on the decision so _add persists it (the
        # Postgres store reads these off the decision the same way).
        decision.source = source
        decision.scope = scope
        decision.category = category
        decision.meta = meta or {}
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
        if decision.dropped:
            return decision
        if decision.embedding is None:
            # No candidate-consuming step ran (e.g. a redact-only policy); still
            # embed once for persistence.
            decision.embedding = self.embedder.embed_one(decision.text)
        # FR-005: never two active facts that contradict — a forced-active write
        # flagged against an already-active fact lands "proposed" (pending).
        demote_active_contradiction(decision)
        if decision.action == "update" and decision.update_target_id:
            self._merge(decision)
            return decision
        if decision.action == "augment" and decision.update_target_id:
            self._augment(decision)
            return decision
        if decision.action == "overwrite" and decision.update_target_id:
            self._overwrite(decision)
            return decision
        self._add(decision)
        self._apply_supersessions(decision)
        return decision

    def _apply_supersessions(self, decision: WriteDecision) -> None:
        """Enact temporal supersession flags (Graphiti invalidate-and-keep).

        ``supersede:<loser>`` retires the older fact; ``supersede_self:<winner>``
        retires the just-added incoming (a backfilled historical fact). Retirement
        is ``state='rejected'`` (the in-memory analogue of closing the bi-temporal
        window) so the loser leaves retrieval and the contradiction surface.
        """
        by_id = {f.id: f for f in self._facts}
        for flag in decision.flags:
            if flag.startswith("supersede:"):
                loser = by_id.get(flag.split(":", 1)[1])
                if loser is not None:
                    loser.state = "rejected"
            elif flag.startswith("supersede_self:"):
                self._facts[-1].state = "rejected"  # the fact _add just appended

    # --- SearchableGraph contract ------------------------------------------
    def search(
        self,
        query: str,
        *,
        top_k: int | None = 10,
        filters: dict | None = None,
        scope: str | None = None,
        state: str | None = "active",
        hybrid: bool = False,
        keyword_weight: float | None = None,
        exclude_categories: list[str] | None = None,
    ) -> list[SearchHit]:
        # hybrid/keyword_weight (gap H7) are no-ops here: the in-memory store has no
        # keyword (BM25) branch to fuse, so retrieval is always pure cosine. Accepted
        # for signature parity with PostgresVectorGraph / the SearchableGraph contract.
        excluded = set(exclude_categories or ())
        candidates = [
            f
            for f in self._facts
            if (scope is None or f.scope == scope)
            and (state is None or f.state == state)
            and (f.category not in excluded)  # H2 exclusion (NULL category never excluded)
            and all(getattr(f, k, None) == v for k, v in (filters or {}).items())
        ]
        if not candidates:
            return []
        qvec = self.embedder.embed_one(query)
        # Outcome/trust weighting: scale cosine similarity by each fact's utility
        # multiplier (neutral 1.0 until outcomes are recorded — no change for
        # un-scored facts; decays toward 0 as a fact's action keeps failing), mirror
        # of PostgresVectorGraph._search_vec. Lets a proven fact beat a more similar
        # but demonstrably-failed one.
        hits = [
            SearchHit(fact=f, score=_cosine(qvec, f.embedding) * f.utility)
            for f in candidates
            if f.embedding is not None
        ]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def record_outcome(self, fact_id: str, *, success: bool) -> None:
        """Feed a downstream verification result back into a fact's trust.

        Increments the fact's ``success_count`` or ``failure_count``; ``search``
        folds them into a utility multiplier so a fact whose suggested action
        repeatedly fails sinks in ranking and a proven one holds. No-op if the id is
        unknown. Mirrors ``PostgresVectorGraph.record_outcome``.
        """
        for f in self._facts:
            if f.id == fact_id:
                if success:
                    f.success_count += 1
                else:
                    f.failure_count += 1
                return

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

    def _recall_semantic(self, decision: WriteDecision) -> None:
        """Fill ``decision.semantic_candidates`` via a wider, lower-floor recall.

        Reuses ``decision.embedding`` (one embedding per write). Returns existing
        facts scoring >= ``semantic_recall_floor`` (below the dedup/conflict floor),
        capped at ``semantic_recall_k``, so the semantic LLM judge can see paraphrase
        contradictions the narrow pass drops. Searches all states, like ``_recall``.
        """
        if decision.embedding is None:
            decision.embedding = self.embedder.embed_one(decision.text)
        if not self._facts:
            return
        hits = [
            SearchHit(fact=f, score=_cosine(decision.embedding, f.embedding))
            for f in self._facts
            if f.embedding is not None
        ]
        hits.sort(key=lambda h: h.score, reverse=True)
        decision.semantic_candidates = [
            h for h in hits if h.score >= self.semantic_recall_floor
        ][: self.semantic_recall_k]

    def _recall_claims(self, decision: WriteDecision) -> None:
        """Fill ``decision.claim_candidates`` with facts sharing a functional slot.

        For each functional claim on the incoming write, find existing facts that
        hold a functional claim on the same normalized (subject, attribute) slot.
        Multi-valued claims are ignored — only functional slots can contradict.
        """
        incoming = [c for c in decision.claims if c.functional]
        if not incoming:
            return
        wanted = {c.slot for c in incoming}
        hits: list[ClaimHit] = []
        for f in self._facts:
            for c in f.claims:
                if c.functional and c.slot in wanted:
                    hits.append(
                        ClaimHit(
                            fact=SearchHit(fact=f, score=1.0),
                            subject=c.slot[0],
                            attribute=c.slot[1],
                            value=c.value,
                        )
                    )
        decision.claim_candidates = hits

    @property
    def facts(self) -> list[Fact]:
        """Stored facts (read-only view for export/adapters)."""
        return list(self._facts)

    # --- internals ----------------------------------------------------------
    def _add(self, decision: WriteDecision) -> None:
        fact_id = uuid.uuid4().hex
        decision.added_fact_id = fact_id  # let callers map this write to its row
        self._facts.append(
            Fact(
                id=fact_id,
                text=decision.text,
                state=decision.state,
                embedding=decision.embedding,  # reuse the vector embedded in _recall
                flags=list(decision.flags),
                tags=list(decision.tags),
                claims=list(decision.claims),
                source=getattr(decision, "source", None),
                scope=getattr(decision, "scope", None),
                category=getattr(decision, "category", None),
                meta=getattr(decision, "meta", None) or {},
            )
        )

    def _merge(self, decision: WriteDecision) -> None:
        for fact in self._facts:
            if fact.id == decision.update_target_id:
                fact.observation_count += 1
                fact.confidence = min(1.0, fact.confidence + 0.05)
                fact.flags.extend(decision.flags)
                return

    def _augment(self, decision: WriteDecision) -> None:
        """Mem0 UPDATE/merge: rewrite the target fact's text to the merged survivor.

        Keeps a single fact: the existing fact identified by ``update_target_id``
        absorbs the new note's content (``augment_text``), re-embeds so retrieval
        tracks the merged text, bumps observation_count, and nudges confidence.
        The incoming note is *not* added as a separate fact.
        """
        merged = (decision.augment_text or decision.text).strip()
        for fact in self._facts:
            if fact.id == decision.update_target_id:
                fact.text = merged
                fact.embedding = self.embedder.embed_one(merged)
                fact.observation_count += 1
                fact.confidence = min(1.0, fact.confidence + 0.05)
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
                fact.claims = list(decision.claims)
            elif fact.id in targets:
                fact.state = "rejected"
