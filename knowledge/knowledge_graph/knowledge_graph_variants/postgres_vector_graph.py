"""Persistent vector store of facts, backed by the RDS ``facts`` table.

The durable sibling of :class:`~knowledge.knowledge_graph.knowledge_graph_variants.vector_graph.VectorGraph`:
same ``KnowledgeGraph``/``SearchableGraph`` contract and the same write-policy
pipeline (redact -> dedup -> conflict), but every fact lives in Postgres under a
single ``(org_id, user_id)`` tenant with a pgvector embedding. Retrieval is a
pgvector cosine search; the read predicate ``org_id = %s AND (shared OR user_id
= %s)`` matches the rest of the multi-tenant schema (see ``migrations/``).

It reuses a connection the caller already holds (the backend opens one shared
autocommit connection per process and injects it), so this class never opens or
closes a connection itself. ``pgvector.psycopg`` is registered on that
connection (see ``serve/db.py``) so embeddings round-trip as plain python lists.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import psycopg
from pgvector import Vector

from knowledge.knowledge_graph.knowledge_graph_def import Claim, Fact, SearchHit
from knowledge.knowledge_graph.parent_searchable_graph import SearchableGraph
from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_policy_def import (
    ClaimHit,
    WriteDecision,
    demote_active_contradiction,
)
from knowledge.knowledge_graph.write_policy.write_step_variants import (
    TABULAR_FLAG,
    AugmentJudge,
    Augmenter,
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
from knowledge.llm.embedder_variants.openrouter_embedder import OpenRouterEmbedder
from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm
from knowledge.llm.parent_embedder import Embedder
from knowledge.llm.parent_llm import Llm
from knowledge.observability import tracing

# The ``facts.embedding`` column is fixed at ``vector(1536)`` (the OpenRouter
# embedding width). Any embedder's vectors are coerced to this width on the way
# in so deterministic test embedders (32-dim FakeEmbedder) round-trip too.
_EMBED_DIM = 1536

# Rough budget for ``read``: cap the concatenated context so a reader prompt
# stays bounded. ~4 chars/token, so this is a few thousand tokens.
_READ_CHAR_BUDGET = 8000

# Reciprocal Rank Fusion constant (Graphiti/Zep use ~60). Larger k flattens the
# weight of top ranks, blending the two branches more gently; 60 is the standard.
_RRF_K = 60

# How deep each branch fetches before fusion. Pulling more than top_k from each
# side lets a fact that is, say, #4 in cosine but #1 in BM25 still win after RRF.
_FUSION_BRANCH_N = 20

# Semantic-dominant fusion. The keyword (BM25) branch is down-weighted in RRF so it
# can RESCUE/nudge a fact (e.g. one carrying a rare identifier) but cannot DEMOTE a
# strong semantic top hit below a keyword-only match on an incidental shared word.
# The audit showed equal-weight fusion let an off-topic doc sharing one ordinary
# token (e.g. "expense"/"team") displace the correct semantic #1 — this guards that.
_RRF_SEMANTIC_WEIGHT = 1.0
_RRF_KEYWORD_WEIGHT = 0.25

# Discriminativeness gate for the keyword branch: a query lexeme only contributes a
# doc's keyword score when it appears in at most this fraction of the tenant's docs.
# A common word (high document frequency) is non-selective and is ignored, so it
# can't pull an off-topic doc into the keyword branch; a rare token (identifier,
# error/runbook code) stays well under the cap and still surfaces its doc.
_KEYWORD_MAX_DF_RATIO = 0.5

# Table names are interpolated directly into SQL (psycopg can't parametrize
# identifiers), so they must NEVER be user-controlled. Only these fixed names
# are permitted: the live-knowledge spine and the saved-state cache.
_ALLOWED_FACTS_TABLES = {"facts", "cached_facts"}
_ALLOWED_EDGES_TABLES = {"fact_edges", "cached_fact_edges"}

# Derivation provenance (gap H5). ``derived_from`` is written learning -> basis:
# ``add_edge(L, F, DERIVED_FROM_EDGE)`` means "L was derived from F" (src=L, dst=F),
# so the dependents of F (suspect when F flips) are the edges where dst_id = F.
# When a source is invalidated, the propagation hook stamps the review edge
# ``STALE_DERIVED_EDGE`` from each transitive dependent -> the rejected source, and
# ``stale_derived()`` reads those (one writer, one reader).
DERIVED_FROM_EDGE = "derived_from"
STALE_DERIVED_EDGE = "derived_source_invalidated"
_MAX_DERIVATION_DEPTH = 25

# Requirement->surface bindings (typed "renders" relation). A SURFACE is a screen
# in the clickable wireframe, modeled AS A FACT (category="surface", scope=<project>,
# text=title|screen_id, meta={"screen_id","title","file","states"}) so it can be an
# endpoint of ``fact_edges`` (the FK requires both endpoints to be facts — no new
# table, no migration). ``add_edge(R, S, RENDERS_EDGE)`` means "requirement R RENDERS
# surface S" (src=requirement fact, dst=surface fact). It reuses the tenant scope,
# idempotency (ON CONFLICT DO NOTHING) and ON DELETE CASCADE of fact_edges for free.
# Queries are active-only (mirror ``active_edges``) so a rejected endpoint drops from
# every result with NO stale hook: a surface does not "go stale" like a derived
# learning, it simply stops being rendered when its requirement or itself is rejected.
RENDERS_EDGE = "renders"            # src=requirement fact, dst=surface fact
SURFACE_CATEGORY = "surface"
REQUIREMENT_CATEGORY = "requirement"

# Episodic memory (gap H4). The reserved category that routes a write down the
# store-only lane (whole, append-only, immutable — never distilled/deduped/
# contradicted) and out of semantic recall (H2). Load-bearing: a non-episode write
# may NOT use it (see `write`), and episodes are produced only via `record_episode`.
EPISODIC_CATEGORY = "episodic"

# Temporal decay (gap H3). Retrieval scales a fact's score by a recency factor
# exp(-ln2 * age / half_life) on ``created_at`` (age = now - created_at), so a stale,
# unconfirmed fact fades vs a fresh one. Neutral (~1.0) for fresh facts, so existing
# behavior is unchanged for anything written recently. Applied to retrieval only —
# NOT to write-time dedup/conflict recall (which must still find old near-dups) nor
# to ``as_of`` point-in-time recall (decay-vs-now would be wrong there).
_RECENCY_HALF_LIFE_DAYS = 90.0
_LN2 = 0.6931471805599453

# Columns copied verbatim between `facts` and `cached_facts` for snapshot
# save/load (everything except the cache_key, which is stamped per copy).
_FACT_COPY_COLS = (
    "id, org_id, user_id, shared, text, source, confidence, scope, category, "
    "observation_count, state, embedding, cluster_id, cluster_label, "
    "valid_at, invalid_at, meta, created_at"
)

# Columns copied verbatim between `claims` and `cached_claims` (cache_key stamped
# per copy). Keeps extracted claims in snapshots/eval cache losslessly.
_CLAIM_COPY_COLS = (
    "org_id, user_id, fact_id, seq, subject, attribute, value, functional, created_at"
)


def default_write_policy(llm: Llm | None = None) -> list[WriteStep]:
    """The baseline pipeline: redact, dedup, extract claims, then detect conflicts.

    The structural contradiction path: ``ClaimExtractor`` decomposes the write into
    (subject, attribute, value) claims and ``ClaimConflictDetector`` flags
    same-functional-slot value clashes. Mirrors ``VectorGraph``'s default; the
    forced-overwrite add path injects a ``ConflictOverwriter`` policy instead.
    ``ClaimExtractor`` runs before ``Deduper`` so the deduper's tabular slot-guard
    can read ``decision.claims``.
    """
    base = llm or OpenRouterLlm()
    return [
        Redactor(),
        # ClaimExtractor runs before Deduper so the deduper's tabular slot-guard can
        # read decision.claims.
        ClaimExtractor(judge=ClaimExtractionJudge(llm=base)),
        Deduper(),
        # Mem0-style UPDATE/merge: fold a related-but-additive note into an existing
        # fact. Runs after Deduper (dups already collapsed) and before the conflict
        # detector (a genuine clash is still flagged, not silently merged).
        # The Augmenter carries a contradiction-precedence guard (a SemanticConflictJudge):
        # it runs before the conflict detectors, so it must refuse to additively merge two
        # facts that contradict -- otherwise a "surface" write silently blends a
        # contradictory newcomer into the incumbent and never flags the pair.
        Augmenter(
            judge=AugmentJudge(llm=base),
            conflict_judge=SemanticConflictJudge(llm=base),
        ),
        ClaimConflictDetector(judge=ClaimValueJudge(llm=base)),
        # Second-pass semantic fallback (Graphiti two-stage): catches paraphrase
        # contradictions among cosine-recalled neighbours that share no slot.
        SemanticConflictDetector(judge=SemanticConflictJudge(llm=base)),
        # Last: reinterpret a dated same-slot value change (e.g. HQ in 2019 vs 2024)
        # as supersession rather than a standing contradiction. Only touches flags
        # the detectors above already raised, so it cannot lower their precision.
        TemporalSupersessionDetector(),
    ]


def _fit(vec: list[float]) -> Vector:
    """Pad/truncate an embedding to the ``facts.embedding`` width as a pgvector.

    Returns a ``pgvector.Vector`` (not a bare list): pgvector's psycopg adapter
    only maps ``Vector``/numpy arrays to the ``vector`` type — a plain list is
    sent as ``double precision[]`` and won't match the ``<=>`` operator.
    """
    if len(vec) > _EMBED_DIM:
        vec = list(vec[:_EMBED_DIM])
    elif len(vec) < _EMBED_DIM:
        vec = list(vec) + [0.0] * (_EMBED_DIM - len(vec))
    return Vector(vec)


def _row_to_hit(r: tuple) -> SearchHit:
    """Build a ``SearchHit`` from a search row.

    Row shape (shared by the cosine and keyword branches):
    ``(id, text, source, confidence, scope, category, observation_count, state, score)``.
    """
    return SearchHit(
        fact=Fact(
            id=r[0],
            text=r[1],
            source=r[2],
            confidence=r[3] if r[3] is not None else 1.0,
            scope=r[4],
            category=r[5],
            observation_count=r[6],
            state=r[7],
        ),
        score=float(r[8]),
    )


def _rrf_fuse(
    semantic: list[SearchHit],
    keyword: list[SearchHit],
    *,
    top_k: int,
    keyword_weight: float = _RRF_KEYWORD_WEIGHT,
) -> list[SearchHit]:
    """Fuse two ranked branches with Reciprocal Rank Fusion, returning top ``top_k``.

    RRF score for a fact is ``Σ 1/(_RRF_K + rank)`` over the branches it appears in
    (rank is 1-based, best first). Position-only, so the (cosine) and (ts_rank)
    score scales never need calibration. The kept ``SearchHit.score`` prefers the
    semantic (cosine) similarity when present — so existing score-threshold callers
    keep meaningful numbers — and falls back to the keyword branch's ts_rank for a
    keyword-only hit. Ties (equal fused score) break toward the semantic branch's
    order, then keyword order, giving a stable, deterministic ranking.

    ``keyword_weight`` (gap H7) is the per-call fusion bias: the semantic branch is
    fixed at ``_RRF_SEMANTIC_WEIGHT`` (1.0) and the keyword branch is scaled by this
    relative weight. Raising it biases toward exact/symbol matches (a concept-vs-symbol
    query knob); the default keeps the calibrated semantic-dominant behavior.
    """
    fused: dict[str, float] = {}
    hit_by_id: dict[str, SearchHit] = {}
    order: dict[str, int] = {}  # first-seen position, for stable tie-breaking
    for branch, weight in ((semantic, _RRF_SEMANTIC_WEIGHT), (keyword, keyword_weight)):
        for rank, hit in enumerate(branch, start=1):
            fid = hit.fact.id
            fused[fid] = fused.get(fid, 0.0) + weight / (_RRF_K + rank)
            order.setdefault(fid, len(order))
            # Prefer the semantic hit's score (cosine similarity) when this fact
            # appeared in the semantic branch; otherwise keep the keyword score.
            if fid not in hit_by_id or branch is semantic:
                hit_by_id[fid] = hit
    ranked = sorted(
        hit_by_id.values(),
        key=lambda h: (-fused[h.fact.id], order[h.fact.id]),
    )
    return ranked[:top_k]


class PostgresVectorGraph(SearchableGraph):
    """A pgvector-backed fact store for one ``(org_id, user_id)`` tenant."""

    def __init__(
        self,
        conn: psycopg.Connection,
        org_id: str,
        user_id: str,
        *,
        embedder: Embedder | None = None,
        policy: list[WriteStep] | None = None,
        recall_floor: float = 0.45,
        recall_k: int = 5,
        semantic_recall_floor: float = 0.30,
        semantic_recall_k: int = 10,
        facts_table: str = "facts",
        edges_table: str = "fact_edges",
        cache_key: str | None = None,
    ) -> None:
        # Validate table names against the allowlist before they ever reach SQL.
        if facts_table not in _ALLOWED_FACTS_TABLES:
            raise ValueError(f"facts_table must be one of {_ALLOWED_FACTS_TABLES}, got {facts_table!r}")
        if edges_table not in _ALLOWED_EDGES_TABLES:
            raise ValueError(f"edges_table must be one of {_ALLOWED_EDGES_TABLES}, got {edges_table!r}")
        # ``cache_key`` binds this graph to one saved state in the cache tables
        # (``cached_facts``/``cached_fact_edges``): every write/edge it makes is
        # stamped with it, and ``wipe_cache`` clears it. It is required for the
        # cache tables (those columns are NOT NULL) and must be None for the live
        # ``facts``/``fact_edges`` tables (which have no cache_key column).
        is_cache = facts_table == "cached_facts"
        if is_cache and cache_key is None:
            raise ValueError("cache_key is required when using the cache tables")
        if not is_cache and cache_key is not None:
            raise ValueError("cache_key must be None for the live facts tables")
        self._facts_table = facts_table
        self._edges_table = edges_table
        # The claims table tracks the facts table: live facts -> `claims`,
        # cached facts -> `cached_claims`. Not caller-controlled, so no allowlist
        # check is needed beyond the facts_table validation above.
        self._claims_table = "cached_claims" if is_cache else "claims"
        self._cache_key = cache_key
        self._conn = conn
        self.org_id = org_id
        self.user_id = user_id
        # Real defaults for production; tests inject deterministic fakes.
        self.embedder = embedder or OpenRouterEmbedder()
        self.policy = policy if policy is not None else default_write_policy()
        # One shared recall gate for both judges (loose, high-recall): the single
        # per-write candidate pass keeps facts scoring >= recall_floor (top recall_k).
        self.recall_floor = recall_floor
        self.recall_k = recall_k
        # Wider, lower-floor recall reserved for the semantic contradiction pass.
        self.semantic_recall_floor = semantic_recall_floor
        self.semantic_recall_k = semantic_recall_k

    # --- KnowledgeGraph contract -------------------------------------------
    def read(
        self,
        context: str | None = None,
        *,
        exclude_categories: list[str] | None = None,
        char_budget: int | None = None,
    ) -> str:
        """Retrieve tenant knowledge: top-k similar to ``context``, else recent.

        Score-ordered and token-bounded so the result is a usable reader prompt.
        ``exclude_categories`` (H2) omits those categories (e.g. ["episodic"]).
        ``char_budget`` (gap H7) is the per-call context size cap; ``None`` uses the
        default ``_READ_CHAR_BUDGET``. A smaller budget keeps the reader prompt tight.
        """
        context = (context or "").strip()
        excluded = set(exclude_categories or ())
        budget = _READ_CHAR_BUDGET if char_budget is None else char_budget
        if context:
            hits = self.search(context, top_k=12, exclude_categories=exclude_categories)
            facts = [h.fact for h in hits]
        else:
            facts = [f for f in self._recent(limit=50) if f.category not in excluded]
        parts: list[str] = []
        used = 0
        for fact in facts:
            if used + len(fact.text) > budget and parts:
                break
            parts.append(fact.text)
            used += len(fact.text)
        return "\n\n".join(parts)

    def write(
        self,
        content: str,
        *,
        state: str = "proposed",
        source: str | None = None,
        scope: str | None = None,
        category: str | None = None,
        meta: dict | None = None,
        tabular: bool = False,
        derived_from: list[str] | None = None,
    ) -> str | None:
        """Run the write-policy pipeline over ``content``, then persist.

        ``state`` ("active" for a direct user approval, "proposed" for a passive
        system add) is the lifecycle state a freshly-added fact lands in.

        Returns the id of the added/updated/merged/overwritten fact, or ``None``
        if the write was dropped (empty or suppressed by a policy step).

        ``source``/``scope``/``category``/``meta`` are carried into persistence.
        Cache writes are stamped with this graph's bound ``cache_key``; ``meta``
        persists into the ``meta`` jsonb column.

        ``derived_from`` records derivation provenance (gap H5): one
        ``derived_from`` edge (this fact -> each source id) so an invalidated source
        can later surface the learnings built on it via ``stale_derived``.

        Side effect: for every existing fact the policy flagged as a
        contradiction (``decision.flags`` entries of the form
        ``"contradiction:<id>"``), a ``contradiction`` edge is inserted from the
        newly-persisted fact to that conflicting fact, so the dashboard
        Contradictions tab works directly off the facts spine.
        """
        decision = self.decide(
            content,
            state=state,
            source=source,
            scope=scope,
            category=category,
            meta=meta,
            tabular=tabular,
            derived_from=derived_from,
        )
        if decision is None:
            return None
        return self.persist(decision)

    def decide(
        self,
        content: str,
        *,
        state: str = "proposed",
        source: str | None = None,
        scope: str | None = None,
        category: str | None = None,
        meta: dict | None = None,
        tabular: bool = False,
        derived_from: list[str] | None = None,
    ) -> WriteDecision | None:
        """Run the read-only half of ``write``: embed, recall, and the policy steps.

        Returns the finished :class:`WriteDecision` (its ``action``/target/embedding
        decided, ``demote_active_contradiction`` applied) ready for :meth:`persist`,
        or ``None`` when the write is empty or a policy step dropped it. Performs **no
        writes** — only ``SELECT``s — so it is safe to run concurrently (each caller
        on its own connection). ``write`` = this then ``persist``; the batch writer
        splits the two to decide in parallel and commit serially without losing
        same-batch dedup (see ``knowledge/serve/batch_writer``).
        """
        content = content.strip()
        if not content:
            return None
        # Reserved-tag integrity (H4 §1c): the episodic category routes to the
        # store-only lane and out of recall, so a normal semantic write must never
        # use it — else the fact silently vanishes from retrieval. Episodes are
        # produced only via record_episode (which bypasses this path).
        if category == EPISODIC_CATEGORY:
            raise ValueError(
                f"category {EPISODIC_CATEGORY!r} is reserved for episodes; use record_episode()"
            )
        decision = WriteDecision(text=content, state="active" if state == "active" else "proposed")
        if tabular:
            decision.flags.append(TABULAR_FLAG)
        # A write carrying derived_from declares a NEW fact built on a source; flag it so
        # the merge steps (Deduper same-lesson / Augmenter additive) keep it distinct, then
        # record_derivation below stamps the edge. Keyed on derived_from presence.
        decision.derived = bool(derived_from)
        # Stash the persistence attributes on the decision so _add/_overwrite
        # (which read them off the decision via getattr) write them through.
        decision.source = source
        decision.scope = scope
        decision.category = category
        decision.meta = meta or {}
        # A declared derivation is a new fact built on its sources, never a duplicate;
        # carry it onto the decision so the Augmenter exempts it from the merge (H5).
        decision.derived_from = list(derived_from or [])
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
            return None
        if decision.embedding is None:
            # No candidate-consuming step ran; still embed once for persistence.
            decision.embedding = self._embed(decision.text)
        # FR-005: never two active facts that contradict. A forced-active write
        # flagged against an already-active fact lands "proposed" (a pending
        # contradiction); the contradiction edge is still persisted below.
        demote_active_contradiction(decision)
        return decision

    def persist(self, decision: WriteDecision) -> str | None:
        """Enact a finished :class:`WriteDecision` (the write half of ``write``).

        Writes through the action ``decide`` settled on (add/merge/augment/overwrite),
        records contradiction edges, and stamps any declared ``derived_from``
        provenance. Must run on a connection that owns the write — the batch writer
        calls this serially on the base connection so each commit is visible to the
        next item's recall.
        """
        if decision.action == "update" and decision.update_target_id:
            self._merge(decision)
            fact_id = decision.update_target_id
        elif decision.action == "augment" and decision.update_target_id:
            self._augment(decision)
            fact_id = decision.update_target_id
        elif decision.action == "overwrite" and decision.update_target_id:
            fact_id = self._overwrite(decision)
        else:
            fact_id = self._add(decision)
        self._persist_contradictions(fact_id, decision)
        if decision.derived_from:
            self.record_derivation(fact_id, decision.derived_from)
        return fact_id

    def sibling(
        self, conn: psycopg.Connection, *, policy: list[WriteStep] | None = None
    ) -> "PostgresVectorGraph":
        """A clone of this graph bound to ``conn`` (same tenant/embedder/recall config).

        Used by the parallel batch writer to give each worker thread its own
        connection — a psycopg connection is not safe for concurrent cross-thread
        use — while preserving identical decide() behavior. ``policy`` defaults to a
        SHARED reference to this graph's policy; pass a fresh policy list when the
        steps hold per-call state. The embedder is shared (thread-safe memo)."""
        return PostgresVectorGraph(
            conn,
            self.org_id,
            self.user_id,
            embedder=self.embedder,
            policy=self.policy if policy is None else policy,
            recall_floor=self.recall_floor,
            recall_k=self.recall_k,
            semantic_recall_floor=self.semantic_recall_floor,
            semantic_recall_k=self.semantic_recall_k,
            facts_table=self._facts_table,
            edges_table=self._edges_table,
            cache_key=self._cache_key,
        )

    def record_episode(
        self,
        text: str,
        *,
        alternatives: list[str] | None = None,
        outcome: str = "pending",
        derived_from: list[str] | None = None,
        decided_at: str | None = None,
    ) -> str | None:
        """Append an episode (a decision + its rationale) — store-only (gap H4).

        An episode is the *storage shape* of a fact but must NOT run the semantic
        write pipeline: no distillation (stored whole, one row), no dedup/merge
        (decisions are a timeline), no contradiction/supersession (a decision stays
        true forever; reversal is recorded as ``outcome``). So this writes the row
        directly via ``_add`` — bypassing the policy entirely — tagged
        ``category="episodic"`` with a ``meta.episode`` block, plus ``derived_from``
        edges (H5) to the facts the decision was based on.

        Empty text is a no-op (returns ``None``). Returns the new fact id.
        """
        text = text.strip()
        if not text:
            return None
        decision = WriteDecision(text=text, state="active")
        decision.source = None
        decision.scope = None
        decision.category = EPISODIC_CATEGORY
        decision.meta = {
            "episode": {
                "decided_at": decided_at or datetime.now(timezone.utc).isoformat(),
                "alternatives": list(alternatives or []),
                "outcome": outcome,
            }
        }
        decision.embedding = self._embed(text)
        fact_id = self._add(decision)  # store-only: no distill/dedup/augment/conflict
        if derived_from:
            self.record_derivation(fact_id, derived_from)
        return fact_id

    def record_derivation(self, fact_id: str, source_ids: list[str]) -> None:
        """Record that ``fact_id`` was derived from each id in ``source_ids``.

        Writes one ``derived_from`` edge (fact_id -> source) per source, so an
        invalidated source can surface ``fact_id`` as suspect (see ``stale_derived``).
        Idempotent (``add_edge`` is ``ON CONFLICT DO NOTHING``); self-edges skipped.
        """
        for src in source_ids:
            if src and src != fact_id:
                self.add_edge(fact_id, src, DERIVED_FROM_EDGE)

    def dependents(
        self, fact_id: str, kind: str = DERIVED_FROM_EDGE, max_depth: int = _MAX_DERIVATION_DEPTH
    ) -> list[Fact]:
        """Transitive dependents of ``fact_id`` along ``kind`` edges (newest first).

        A ``kind`` edge is src=dependent -> dst=basis, so the dependents of
        ``fact_id`` are the rows where ``dst_id = fact_id``; their ``src_id`` is the
        dependent, recursed as the next ``dst``. Cycle-guarded (a fact never revisits
        an id already on its path) and depth-bounded.
        """
        ids = self._dependent_ids(fact_id, kind, max_depth)
        if not ids:
            return []
        return [f for f in self.all_facts() if f.id in ids]

    def _dependent_ids(self, fact_id: str, kind: str, max_depth: int) -> set[str]:
        cache = ""
        if self._cache_key is not None:
            cache = " AND cache_key = %s"
        # Recursive walk up the derivation chain (dst -> src), carrying the visited
        # path to break cycles, bounded by ``max_depth``.
        base_params = [self.org_id, self.user_id, kind]
        sql = (
            "WITH RECURSIVE deps(id, depth, path) AS ("
            f"  SELECT src_id, 1, ARRAY[src_id] FROM {self._edges_table} "
            f"   WHERE org_id=%s AND user_id=%s AND kind=%s AND dst_id=%s{cache} "
            "  UNION ALL "
            "  SELECT e.src_id, d.depth+1, d.path || e.src_id "
            f"   FROM {self._edges_table} e JOIN deps d ON e.dst_id = d.id "
            f"   WHERE e.org_id=%s AND e.user_id=%s AND e.kind=%s{cache} "
            "     AND d.depth < %s AND NOT (e.src_id = ANY(d.path))"
            ") SELECT DISTINCT id FROM deps"
        )
        anchor = [*base_params, fact_id]
        if self._cache_key is not None:
            anchor.append(self._cache_key)
        recur = [*base_params]
        if self._cache_key is not None:
            recur.append(self._cache_key)
        recur.append(max_depth)
        rows = self._conn.execute(sql, [*anchor, *recur]).fetchall()
        return {r[0] for r in rows}

    def _flag_stale_dependents(self, fact_id: str) -> None:
        """Propagation hook: a fact was invalidated — flag its transitive derivation
        dependents for review (precision-first: flag, never auto-reject).

        Stamps a ``STALE_DERIVED_EDGE`` review edge (dependent -> the invalidated
        source) on every transitive ``derived_from`` dependent, so ``stale_derived``
        surfaces the whole closure even where an intermediate link is flagged (not
        itself rejected).
        """
        for dep_id in self._dependent_ids(fact_id, DERIVED_FROM_EDGE, _MAX_DERIVATION_DEPTH):
            self.add_edge(dep_id, fact_id, STALE_DERIVED_EDGE)

    def _stale_flagged_ids(self) -> set[str]:
        """Ids of facts carrying a ``STALE_DERIVED_EDGE`` review edge (flagged stale).

        One source of truth for "which facts were flagged stale" — read by both
        ``stale_derived`` (the review surface) and the completeness queries (a stale
        requirement is incomplete: a dependency changed, it needs rework).
        """
        cache = ""
        params: list[object] = [self.org_id, self.user_id, STALE_DERIVED_EDGE]
        if self._cache_key is not None:
            cache = " AND cache_key = %s"
            params.append(self._cache_key)
        rows = self._conn.execute(
            f"SELECT DISTINCT src_id FROM {self._edges_table} "
            f"WHERE org_id=%s AND user_id=%s AND kind=%s{cache}",
            params,
        ).fetchall()
        return {r[0] for r in rows}

    def stale_derived(self) -> list[Fact]:
        """Active facts flagged stale because a fact they derive from was invalidated.

        Reads the ``STALE_DERIVED_EDGE`` review edges set by ``_flag_stale_dependents``
        (one reader, one writer). Returns the suspect learnings for human/agent review.
        """
        ids = self._stale_flagged_ids()
        return [f for f in self.all_facts(state="active") if f.id in ids]

    def _persist_contradictions(self, fact_id: str, decision: WriteDecision) -> None:
        """Materialize the policy's ``contradiction:<id>`` flags as edges.

        ``ConflictFlagger`` records each detected conflict as a flag string
        ``"contradiction:<conflicting_fact_id>"`` on ``decision.flags`` (see
        ``write_step_variants/conflict_flagger.py``). We turn each into a
        persisted edge from the new fact to the conflicting one.
        """
        for flag in decision.flags:
            if flag.startswith("contradiction:"):
                conflict_id = flag.split(":", 1)[1]
                if conflict_id and conflict_id != fact_id:
                    self.add_edge(fact_id, conflict_id, "contradiction")
            elif flag.startswith("supersede:"):
                # Temporal supersession (incoming newer): retire the older fact —
                # close its validity window + mark rejected — and record lineage.
                loser = flag.split(":", 1)[1]
                if loser and loser != fact_id:
                    self._supersede(winner_id=fact_id, loser_id=loser)
            elif flag.startswith("supersede_self:"):
                # Incoming is a backfilled historical fact: it lands already
                # superseded by the existing newer fact, which stays current.
                winner = flag.split(":", 1)[1]
                if winner and winner != fact_id:
                    self._supersede(winner_id=winner, loser_id=fact_id)

    def _supersede(self, *, winner_id: str, loser_id: str) -> None:
        """Retire ``loser_id`` in favor of ``winner_id`` (Graphiti invalidate-and-keep).

        Closes the loser's bi-temporal window (``invalidate``), marks it ``rejected``
        so it leaves the active/contradiction surfaces, and records a ``supersedes``
        edge winner -> loser. The row is kept for point-in-time recall.
        """
        self.invalidate(loser_id, winner_id=winner_id)
        self._conn.execute(
            f"UPDATE {self._facts_table} SET state = 'rejected' "
            "WHERE org_id = %s AND user_id = %s AND id = %s",
            (self.org_id, self.user_id, loser_id),
        )
        self.add_edge(winner_id, loser_id, "supersedes")
        self._flag_stale_dependents(loser_id)  # H5: propagate to derived learnings

    def _persist_claims(self, fact_id: str, claims: list[Claim]) -> None:
        """Replace the stored claims for ``fact_id`` with ``claims``.

        Delete-then-insert so a rewritten fact's claims stay in sync. Subject and
        attribute are stored normalized (the slot index matches on them); value is
        raw. Cache-bound graphs stamp the bound ``cache_key``.
        """
        cache_cols = ["cache_key"] if self._cache_key is not None else []
        del_sql = (
            f"DELETE FROM {self._claims_table} "
            "WHERE org_id=%s AND user_id=%s AND fact_id=%s"
        )
        del_params: list[object] = [self.org_id, self.user_id, fact_id]
        if self._cache_key is not None:
            del_sql += " AND cache_key=%s"
            del_params.append(self._cache_key)
        self._conn.execute(del_sql, del_params)
        for seq, c in enumerate(claims):
            cols = ["org_id", "user_id", *cache_cols, "fact_id", "seq",
                    "subject", "attribute", "value", "functional"]
            vals: list[object] = [self.org_id, self.user_id]
            if self._cache_key is not None:
                vals.append(self._cache_key)
            vals += [fact_id, seq, Claim.norm(c.subject), Claim.norm(c.attribute),
                     c.value, c.functional]
            placeholders = ", ".join(["%s"] * len(vals))
            self._conn.execute(
                f"INSERT INTO {self._claims_table} ({', '.join(cols)}) "
                f"VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                vals,
            )

    def claims_for(self, fact_id: str) -> list[Claim]:
        """Stored claims for one fact, in seq order."""
        sql = (
            f"SELECT subject, attribute, value, functional FROM {self._claims_table} "
            "WHERE org_id=%s AND user_id=%s AND fact_id=%s"
        )
        params: list[object] = [self.org_id, self.user_id, fact_id]
        if self._cache_key is not None:
            sql += " AND cache_key=%s"
            params.append(self._cache_key)
        sql += " ORDER BY seq"
        rows = self._conn.execute(sql, params).fetchall()
        return [Claim(subject=r[0], attribute=r[1], value=r[2], functional=r[3]) for r in rows]

    # --- SearchableGraph contract ------------------------------------------
    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        filters: dict | None = None,
        scope: str | None = None,
        state: str | None = "active",
        as_of: datetime | None = None,
        hybrid: bool = False,
        keyword_weight: float | None = None,
        exclude_categories: list[str] | None = None,
        decay: bool = True,
    ) -> list[SearchHit]:
        """Retrieve relevant facts. Pure pgvector cosine by default; ``hybrid=True``
        additionally fuses a BM25 keyword branch.

        ``keyword_weight`` (gap H7) tunes the RRF fusion bias per call (only meaningful
        with ``hybrid=True``): the semantic branch stays at weight 1.0 and the keyword
        branch is scaled by this value. ``None`` uses the calibrated default
        (``_RRF_KEYWORD_WEIGHT``); raise it to favor exact/symbol matches for a
        symbol-style query, lower it to lean more semantic.

        ``exclude_categories`` (gap H2) omits rows whose ``category`` is in the list
        (NULL category is never excluded), applied to BOTH branches via ``_where`` —
        e.g. ``["episodic"]`` keeps decision logs out of semantic recall.

        ``decay`` (gap H3, default on) scales the cosine score by a recency factor so a
        stale fact fades vs a fresh one; it is suppressed for ``as_of`` recall (decay
        relative to now() would distort point-in-time results).

        Borrows Graphiti/Zep's search stack. Two indexed branches run over the same
        tenant/state/scope/filter predicate:

          * **semantic** — pgvector cosine (HNSW index on ``embedding``), the default.
          * **keyword (BM25-style)** — Postgres full-text over the generated
            ``text_tsv`` column (GIN index), scored by the BM25 IDF component so a
            rare exact term/identifier (token, name, error/runbook code) outranks
            common-word matches.

        Fused with **Reciprocal Rank Fusion** (``Σ weight/(_RRF_K + rank)``). Two
        guards keep keyword noise from hurting precision: a *discriminativeness gate*
        (``_KEYWORD_MAX_DF_RATIO`` — common, low-IDF lexemes never admit a doc to the
        keyword branch) and a *down-weighted* keyword branch (``_RRF_KEYWORD_WEIGHT``
        < semantic) so keyword can rescue/nudge but is unlikely to demote a strong
        semantic hit.

        **Why hybrid is OFF by default (evidence-based).** A larger live eval on this
        embedding stack found the cosine branch already ranks rare identifiers #1, so
        keyword fusion yielded *no* wins, while additive fusion can still demote a
        correct semantic answer that happens to share no query keyword (a keyword-only
        match on an incidental shared word gets a bonus the keyword-absent winner can't
        match). Net: on-by-default fusion was a precision regression. Hybrid remains
        available opt-in for keyword-centric lookups (e.g. searching by an exact error
        code). Making it safe to default-on would need a promotion-only / score-aware
        fusion, not the additive RRF here.

        The internal candidate-recall pass (``_recall`` -> ``_search_vec``) is always
        pure-semantic — dedup/conflict want embedding recall, not keyword matching.
        """
        qvec = _fit(self._embed(query))
        # Decay applies to retrieval ranking, but never to point-in-time (as_of) recall.
        apply_decay = decay and as_of is None
        if not hybrid:
            return self._search_vec(
                qvec, top_k=top_k, filters=filters, scope=scope, state=state, as_of=as_of,
                exclude_categories=exclude_categories, apply_decay=apply_decay,
            )
        sem = self._search_vec(
            qvec, top_k=_FUSION_BRANCH_N, filters=filters, scope=scope, state=state, as_of=as_of,
            exclude_categories=exclude_categories, apply_decay=apply_decay,
        )
        kw = self._search_keyword(
            query, top_k=_FUSION_BRANCH_N, filters=filters, scope=scope, state=state, as_of=as_of,
            exclude_categories=exclude_categories,
        )
        kww = _RRF_KEYWORD_WEIGHT if keyword_weight is None else keyword_weight
        return _rrf_fuse(sem, kw, top_k=top_k, keyword_weight=kww)

    def _where(
        self,
        *,
        filters: dict | None,
        scope: str | None,
        state: str | None,
        as_of: datetime | None = None,
        exclude_categories: list[str] | None = None,
    ) -> tuple[str, list[object]]:
        """The shared tenant/cache/state/scope/filter predicate for a search branch.

        Returns ``(sql_fragment, params)`` so the cosine and keyword branches apply
        the exact same row gating — only their ranking expression differs.
        """
        sql = "WHERE org_id = %s AND (shared OR user_id = %s)"
        params: list[object] = [self.org_id, self.user_id]
        # Bi-temporal validity (Graphiti/Zep): both the semantic and keyword
        # branches gate on the same validity window. Default returns only
        # currently-valid facts; `as_of` rewinds to point-in-time recall. A NULL
        # valid_at is treated as always-valid (legacy rows).
        if as_of is not None:
            sql += (
                " AND (valid_at IS NULL OR valid_at <= %s) "
                "AND (invalid_at IS NULL OR invalid_at > %s)"
            )
            params.extend([as_of, as_of])
        else:
            sql += " AND (invalid_at IS NULL OR invalid_at > now())"
        if self._cache_key is not None:
            # A cache-bound graph only ever sees its own partition, so recall /
            # dedup / conflict stay within the one eval case (and contradiction
            # edges it persists always land between facts in this partition).
            sql += " AND cache_key = %s"
            params.append(self._cache_key)
        if state is not None:
            sql += " AND state = %s"
            params.append(state)
        if scope is not None:
            sql += " AND scope = %s"
            params.append(scope)
        for key, value in (filters or {}).items():
            sql += f" AND {key} = %s"
            params.append(value)
        # H2 exclusion: omit listed categories (NULL category is never excluded).
        if exclude_categories:
            sql += " AND (category IS NULL OR category <> ALL(%s))"
            params.append(list(exclude_categories))
        return sql, params

    def _search_vec(
        self,
        qvec: Vector,
        *,
        top_k: int = 10,
        filters: dict | None = None,
        scope: str | None = None,
        state: str | None = "active",
        as_of: datetime | None = None,
        exclude_categories: list[str] | None = None,
        apply_decay: bool = False,
    ) -> list[SearchHit]:
        where, where_params = self._where(
            filters=filters, scope=scope, state=state, as_of=as_of,
            exclude_categories=exclude_categories,
        )
        # Outcome/trust weighting (H1): cosine similarity scaled by a per-fact utility
        # multiplier from recorded outcomes — neutral 1.0 until outcomes exist.
        # Temporal decay (H3, ``apply_decay``): a recency factor exp(-ln2*age/half_life)
        # so a stale fact fades vs a fresh one — neutral (~1.0) for recently-written
        # facts, so un-aged facts are unaffected. Ordering on the scaled score sorts the
        # matched partition rather than riding the HNSW index (fine for tenant recall).
        decay_sql = (
            " * CASE WHEN created_at IS NULL THEN 1.0 ELSE "
            f"exp(- {_LN2} * EXTRACT(EPOCH FROM (now() - created_at)) "
            f"/ (86400.0 * {_RECENCY_HALF_LIFE_DAYS})) END"
        ) if apply_decay else ""
        sql = (
            "SELECT id, text, source, confidence, scope, category, observation_count, state, "
            "(1 - (embedding <=> %s)) * CASE WHEN success_count + failure_count = 0 THEN 1.0 "
            "ELSE (success_count + 0.5) / (success_count + failure_count + 1.0) END"
            f"{decay_sql} AS score "
            f"FROM {self._facts_table} {where} AND embedding IS NOT NULL "
            "ORDER BY score DESC LIMIT %s"
        )
        params: list[object] = [qvec, *where_params, top_k]
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_hit(r) for r in rows]

    def record_outcome(self, fact_id: str, *, success: bool) -> None:
        """Feed a downstream verification result back into a fact's trust.

        Increments ``success_count`` or ``failure_count`` for ``fact_id`` within this
        graph's tenant. ``search`` folds the counts into a utility multiplier so a
        fact whose suggested action repeatedly fails sinks in ranking and a proven
        one holds — the compounding signal (verified-good knowledge sharpens recall,
        verified-bad knowledge fades) the store otherwise lacks.

        Also stamps ``last_outcome`` with the *latest* result ('succeeded'|'failed').
        The cumulative counts can't tell a once-passing requirement that later
        regressed from one still passing; the latest-outcome signal can, and the
        derived-completeness queries (``incomplete_requirements``) read it to mark a
        succeeded-then-failed requirement as regressed.
        """
        column = "success_count" if success else "failure_count"
        outcome = "succeeded" if success else "failed"
        self._conn.execute(
            f"UPDATE {self._facts_table} SET {column} = {column} + 1, last_outcome = %s "
            "WHERE id = %s AND org_id = %s AND user_id = %s",
            (outcome, fact_id, self.org_id, self.user_id),
        )

    def _search_keyword(
        self,
        query: str,
        *,
        top_k: int = 10,
        filters: dict | None = None,
        scope: str | None = None,
        state: str | None = "active",
        as_of: datetime | None = None,
        exclude_categories: list[str] | None = None,
    ) -> list[SearchHit]:
        """BM25-style keyword branch: IDF-weighted full-text match over ``text_tsv``.

        The point of the keyword branch is to surface a fact carrying a rare, exact
        term/identifier (a token, name, error/runbook code) that cosine ranks out of
        top-k. Postgres ``ts_rank`` has no IDF — it rewards docs matching many query
        words, so common words (here every "on-call engineer" distractor) outrank the
        one doc with the rare identifier, defeating the purpose. So this scores each
        candidate by the **BM25 IDF component**: for every query lexeme a doc shares,
        add ``ln(N / df)`` (N = corpus size in this tenant partition, df = how many
        docs contain that lexeme). A rare token like ``rbk``/``-7782`` (df=1) carries
        a large weight; ubiquitous words carry near-zero — so the identifier fact
        wins. The query is lexed with ``to_tsvector('english', …)`` (same config as
        the stored column) and OR-matched via array intersection on the GIN-indexed
        ``text_tsv``; empty/stopword-only queries yield nothing (RRF then degrades to
        the cosine branch alone). Score is the summed IDF; fusion ranks on position,
        so its scale never needs to match cosine.
        """
        where, where_params = self._where(
            filters=filters, scope=scope, state=state, as_of=as_of,
            exclude_categories=exclude_categories,
        )
        sql = (
            "WITH params AS (SELECT tsvector_to_array(to_tsvector('english', %s)) AS qlex), "
            "docs AS ("
            "  SELECT id, text, source, confidence, scope, category, observation_count, state, "
            "         tsvector_to_array(text_tsv) AS dlex "
            f"  FROM {self._facts_table} {where} AND text_tsv IS NOT NULL"
            "), "
            "ndoc AS (SELECT count(*)::float AS n FROM docs), "
            "matched AS ("
            "  SELECT d.id, unnest(ARRAY(SELECT unnest(d.dlex) INTERSECT SELECT unnest(p.qlex))) AS lex "
            "  FROM docs d CROSS JOIN params p"
            "), "
            "df AS (SELECT lex, count(*)::float AS df FROM matched GROUP BY lex) "
            # Only discriminative lexemes (df <= max_df_ratio * N) count toward a
            # doc's keyword score; common words are ignored so they can't surface an
            # off-topic doc. CASE-gate inside the sum (not a row filter) so a doc that
            # matched ONLY common words scores 0 and is dropped by the HAVING.
            "SELECT d.id, d.text, d.source, d.confidence, d.scope, d.category, "
            "       d.observation_count, d.state, "
            "       COALESCE(sum(CASE WHEN df.df <= %s * ndoc.n "
            "                         THEN ln(ndoc.n / df.df) ELSE 0 END), 0) AS score "
            "FROM docs d CROSS JOIN ndoc "
            "LEFT JOIN matched m ON m.id = d.id "
            "LEFT JOIN df ON df.lex = m.lex "
            "GROUP BY d.id, d.text, d.source, d.confidence, d.scope, d.category, "
            "         d.observation_count, d.state "
            "HAVING COALESCE(sum(CASE WHEN df.df <= %s * ndoc.n "
            "                        THEN ln(ndoc.n / df.df) ELSE 0 END), 0) > 0 "
            "ORDER BY score DESC LIMIT %s"
        )
        params: list[object] = [
            query, *where_params, _KEYWORD_MAX_DF_RATIO, _KEYWORD_MAX_DF_RATIO, top_k
        ]
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_hit(r) for r in rows]

    def overlay_search(
        self,
        query: str,
        mounts: list[tuple[str, str]],
        *,
        top_k: int = 10,
        exclude_categories: list[str] | None = None,
    ) -> list[SearchHit]:
        """Vector-search the live graph unioned with mounted snapshots, in one query.

        Backs the mounted read-only overlay (see ``overlay_graph.py``). ``mounts``
        is a list of ``(source_user_id, cache_key)`` pairs naming saved snapshots
        to also expose. The query is embedded **once** and a single ``UNION ALL``
        ranks the live ``facts`` branch and the ``cached_facts`` branch together —
        no per-mount round trip, no re-embedding. Both tables have an HNSW
        embedding index, so each branch is a sub-linear indexed search.

        The live branch keeps the normal tenant predicate
        (``org_id AND (shared OR user_id)``); the mounted branch is org-scoped but
        cross-user (``org_id AND (user_id, cache_key) ∈ mounts``) — the same
        within-org trust boundary :class:`OrgSourceReader` relies on, with org
        membership validated by the mount route. Mounted hits carry
        ``fact.meta["mountedFrom"]`` so callers can tell them from live facts.
        Results are deduped by id (a live fact wins over a same-id snapshot copy),
        ranked by score, and truncated to ``top_k``.
        """
        qvec = _fit(self._embed(query))
        cols = (
            "id, text, source, confidence, scope, category, observation_count, state"
        )
        # H2: apply category exclusion to BOTH the live and mounted branches so a
        # mounted snapshot's episodes don't leak into the unioned result.
        excl = " AND (category IS NULL OR category <> ALL(%s))" if exclude_categories else ""
        excl_param = [list(exclude_categories)] if exclude_categories else []
        live = (
            f"SELECT {cols}, NULL::text AS mount_user, NULL::text AS mount_key, "
            "1 - (embedding <=> %s) AS score FROM facts "
            "WHERE org_id = %s AND (shared OR user_id = %s) "
            f"AND state = 'active' AND embedding IS NOT NULL{excl} "
            "ORDER BY embedding <=> %s LIMIT %s"
        )
        params: list[object] = [qvec, self.org_id, self.user_id, *excl_param, qvec, top_k]
        sql = f"SELECT * FROM ({live}) AS live"
        if mounts:
            ors = " OR ".join(["(user_id = %s AND cache_key = %s)"] * len(mounts))
            mounted = (
                f"SELECT {cols}, user_id AS mount_user, cache_key AS mount_key, "
                "1 - (embedding <=> %s) AS score FROM cached_facts "
                f"WHERE org_id = %s AND ({ors}) "
                f"AND state = 'active' AND embedding IS NOT NULL{excl} "
                "ORDER BY embedding <=> %s LIMIT %s"
            )
            sql += f" UNION ALL SELECT * FROM ({mounted}) AS mounted"
            params += [qvec, self.org_id]
            for source_user_id, cache_key in mounts:
                params += [source_user_id, cache_key]
            params += [*excl_param, qvec, top_k]
        rows = self._conn.execute(sql, params).fetchall()

        # Each branch is capped at top_k, so this is at most ~2*top_k rows. Dedupe
        # by id preferring the live copy (mount_user IS NULL), then rank + cap.
        best: dict[str, SearchHit] = {}
        for r in rows:
            mount_user, mount_key, score = r[8], r[9], float(r[10])
            hit = SearchHit(
                fact=Fact(
                    id=r[0], text=r[1], source=r[2],
                    confidence=r[3] if r[3] is not None else 1.0,
                    scope=r[4], category=r[5], observation_count=r[6], state=r[7],
                ),
                score=score,
            )
            if mount_user is not None:
                hit.fact.meta["mountedFrom"] = {
                    "userId": mount_user,
                    "snapshot": mount_key.split("snapshot:", 1)[-1],
                }
            existing = best.get(hit.fact.id)
            if existing is None:
                best[hit.fact.id] = hit
                continue
            # Prefer a live hit over a same-id snapshot copy; else higher score.
            existing_mounted = bool(existing.fact.meta.get("mountedFrom"))
            this_mounted = mount_user is not None
            if existing_mounted and not this_mounted:
                best[hit.fact.id] = hit
            elif existing_mounted == this_mounted and hit.score > existing.score:
                best[hit.fact.id] = hit
        ranked = sorted(best.values(), key=lambda h: h.score, reverse=True)
        return ranked[:top_k]

    def recent_cache(
        self, *, source_user_id: str, cache_key: str, limit: int
    ) -> list[Fact]:
        """Newest active facts of a snapshot — the no-query overlay read path."""
        rows = self._conn.execute(
            "SELECT id, text, source, confidence, scope, category, observation_count, state "
            "FROM cached_facts WHERE org_id = %s AND user_id = %s AND cache_key = %s "
            "AND state = 'active' ORDER BY created_at DESC LIMIT %s",
            (self.org_id, source_user_id, cache_key, limit),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    # --- dashboard snapshot (the graph the dashboard renders) --------------
    def active_facts(self) -> list[Fact]:
        """Every ``active`` fact for this tenant — the graph ``search`` reads.

        This is the one-to-one source for the dashboard graph view: the same
        rows ``read``/``search`` retrieve from, so what the dashboard shows is
        exactly what MCP ``get_context`` can recall (newest-first for display).
        """
        rows = self._conn.execute(
            "SELECT id, text, source, confidence, scope, category, observation_count, "
            "state, created_at, meta, cluster_id, cluster_label "
            f"FROM {self._facts_table} WHERE org_id = %s AND (shared OR user_id = %s) "
            "AND state = 'active' ORDER BY created_at DESC",
            (self.org_id, self.user_id),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def active_edges(self) -> list[tuple[str, str, str]]:
        """``(src, dst, kind)`` edges between this tenant's active facts."""
        rows = self._conn.execute(
            f"SELECT e.src_id, e.dst_id, e.kind FROM {self._edges_table} e "
            f"JOIN {self._facts_table} s ON s.org_id = e.org_id AND s.user_id = e.user_id AND s.id = e.src_id "
            f"JOIN {self._facts_table} d ON d.org_id = e.org_id AND d.user_id = e.user_id AND d.id = e.dst_id "
            "WHERE e.org_id = %s AND (s.shared OR s.user_id = %s) "
            "AND s.state = 'active' AND d.state = 'active'",
            (self.org_id, self.user_id),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    # --- clustering (navigation-only topic super-nodes) --------------------
    def recluster(self, *, min_cluster_size: int | None = None) -> int:
        """Define-pass: (re)assign topic clusters over this graph's facts, persisted.

        Runs the embed -> reduce -> HDBSCAN -> label pipeline over every fact in
        this partition (the live tenant graph, or one ``cache_key`` slice) and
        writes the resulting ``cluster_id``/``cluster_label`` back to each row.
        This is the slow, write-time "define" side (used by the pipeline /
        snapshot paths), NOT a per-render call: the assignments are persisted so
        retrieval never re-clusters and the cache copy carries them verbatim.

        Cluster ids are not stable across runs — a settled design decision; the
        graph view groups by whatever ids/labels are current. Returns the number
        of clusters found (0 when there are too few facts or no embedding key, in
        which case every fact is cleared to unclustered).
        """
        from knowledge.knowledge_graph.clustering import MIN_CLUSTER_SIZE, assign_clusters

        facts = self.all_facts()
        if not facts:
            return 0
        n = assign_clusters(
            facts, min_cluster_size=min_cluster_size or MIN_CLUSTER_SIZE
        )
        sql = (
            f"UPDATE {self._facts_table} SET cluster_id = %s, cluster_label = %s "
            "WHERE org_id = %s AND user_id = %s AND id = %s"
        )
        cache_clause = ""
        if self._cache_key is not None:
            cache_clause = " AND cache_key = %s"
        for fact in facts:
            params: list[object] = [
                fact.cluster_id,
                fact.cluster_label,
                self.org_id,
                self.user_id,
                fact.id,
            ]
            if self._cache_key is not None:
                params.append(self._cache_key)
            self._conn.execute(sql + cache_clause, params)
        return n

    # --- full-lifecycle reads (candidate-facade surface) -------------------
    @staticmethod
    def _row_to_fact(r: tuple) -> Fact:
        """Build a Fact from a row of (id,text,source,confidence,scope,category,
        observation_count,state[,created_at[,meta[,cluster_id,cluster_label]]]).

        ``created_at``/``meta``/``cluster_id``/``cluster_label`` are optional
        trailing columns; when present, ``created_at`` is serialized to ISO 8601
        and ``meta`` is normalized to a dict (psycopg returns jsonb as a dict
        already, but tolerate strings).
        """
        created_at = None
        meta: dict = {}
        if len(r) > 8 and r[8] is not None:
            created_at = r[8].isoformat() if hasattr(r[8], "isoformat") else str(r[8])
        if len(r) > 9 and r[9] is not None:
            meta = r[9] if isinstance(r[9], dict) else json.loads(r[9])
        cluster_id = r[10] if len(r) > 10 else None
        cluster_label = r[11] if len(r) > 11 else None
        return Fact(
            id=r[0],
            text=r[1],
            source=r[2],
            confidence=r[3] if r[3] is not None else 1.0,
            scope=r[4],
            category=r[5],
            observation_count=r[6],
            state=r[7],
            cluster_id=cluster_id,
            cluster_label=cluster_label,
            created_at=created_at,
            meta=meta,
        )

    def all_facts(self, state: str | None = None) -> list[Fact]:
        """Every fact for this tenant (optionally filtered by ``state``), newest first."""
        sql = (
            "SELECT id, text, source, confidence, scope, category, observation_count, "
            "state, created_at, meta, cluster_id, cluster_label "
            f"FROM {self._facts_table} WHERE org_id = %s AND (shared OR user_id = %s)"
        )
        params: list[object] = [self.org_id, self.user_id]
        if self._cache_key is not None:
            sql += " AND cache_key = %s"
            params.append(self._cache_key)
        if state is not None:
            sql += " AND state = %s"
            params.append(state)
        sql += " ORDER BY created_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def get_fact(self, fact_id: str) -> Fact | None:
        rows = self._conn.execute(
            "SELECT id, text, source, confidence, scope, category, observation_count, "
            "state, created_at, meta, cluster_id, cluster_label "
            f"FROM {self._facts_table} WHERE org_id = %s AND user_id = %s AND id = %s",
            (self.org_id, self.user_id, fact_id),
        ).fetchall()
        if not rows:
            return None
        return self._row_to_fact(rows[0])

    def set_state(self, fact_id: str, state: str) -> None:
        self._conn.execute(
            f"UPDATE {self._facts_table} SET state = %s "
            "WHERE org_id = %s AND user_id = %s AND id = %s",
            (state, self.org_id, self.user_id, fact_id),
        )
        # Derivation propagation (H5): retiring a fact flags the learnings derived
        # from it for review. This is the candidate-reject / promote-rival chokepoint.
        if state == "rejected":
            self._flag_stale_dependents(fact_id)

    def invalidate(self, fact_id: str, winner_id: str | None = None) -> None:
        """Close a fact's bi-temporal validity window (Graphiti/Zep model).

        Sets ``invalid_at`` to the winner's ``valid_at`` when a superseding
        ``winner_id`` is given (falling back to ``now()``), marking the fact no
        longer currently true while keeping the row for point-in-time recall.
        This is additive to the ``rejected`` state set on the resolve paths;
        callers still flip ``state`` separately. Idempotent-safe to call on an
        already-invalidated row (it just re-stamps the window close).
        """
        if winner_id is not None:
            self._conn.execute(
                f"UPDATE {self._facts_table} AS loser "
                "SET invalid_at = COALESCE("
                f"  (SELECT winner.valid_at FROM {self._facts_table} AS winner "
                "    WHERE winner.org_id = loser.org_id "
                "      AND winner.user_id = loser.user_id AND winner.id = %s), "
                "  now()) "
                "WHERE loser.org_id = %s AND loser.user_id = %s AND loser.id = %s",
                (winner_id, self.org_id, self.user_id, fact_id),
            )
        else:
            self._conn.execute(
                f"UPDATE {self._facts_table} SET invalid_at = now() "
                "WHERE org_id = %s AND user_id = %s AND id = %s",
                (self.org_id, self.user_id, fact_id),
            )

    def update_fact(
        self,
        fact_id: str,
        *,
        text: str | None = None,
        source: str | None = None,
        confidence: float | None = None,
        meta: dict | None = None,
        category: str | None = None,
    ) -> None:
        """Patch a fact's fields. Re-embeds when ``text`` changes."""
        sets: list[str] = []
        params: list[object] = []
        if text is not None:
            sets.append("text = %s")
            params.append(text)
            sets.append("embedding = %s")
            params.append(_fit(self._embed(text)))
        if source is not None:
            sets.append("source = %s")
            params.append(source)
        if confidence is not None:
            sets.append("confidence = %s")
            params.append(confidence)
        if meta is not None:
            sets.append("meta = %s")
            params.append(json.dumps(meta))
        if category is not None:
            sets.append("category = %s")
            params.append(category)
        if not sets:
            return
        params.extend([self.org_id, self.user_id, fact_id])
        self._conn.execute(
            f"UPDATE {self._facts_table} SET {', '.join(sets)} "
            "WHERE org_id = %s AND user_id = %s AND id = %s",
            params,
        )

    def set_meta(self, fact_id: str, meta: dict) -> None:
        self._conn.execute(
            f"UPDATE {self._facts_table} SET meta = %s "
            "WHERE org_id = %s AND user_id = %s AND id = %s",
            (json.dumps(meta), self.org_id, self.user_id, fact_id),
        )

    def delete_fact(self, fact_id: str) -> None:
        self._conn.execute(
            f"DELETE FROM {self._facts_table} WHERE org_id = %s AND user_id = %s AND id = %s",
            (self.org_id, self.user_id, fact_id),
        )

    # --- edges (full lifecycle, candidate-facade surface) ------------------
    def add_edge(self, src_id: str, dst_id: str, kind: str = "contradiction") -> None:
        cols = ["org_id", "user_id", "src_id", "dst_id", "kind"]
        vals: list[object] = [self.org_id, self.user_id, src_id, dst_id, kind]
        if self._cache_key is not None:
            cols.insert(2, "cache_key")
            vals.insert(2, self._cache_key)
        placeholders = ", ".join(["%s"] * len(vals))
        self._conn.execute(
            f"INSERT INTO {self._edges_table} ({', '.join(cols)}) "
            f"VALUES ({placeholders}) ON CONFLICT DO NOTHING",
            vals,
        )

    def remove_edge(self, src_id: str, dst_id: str, kind: str = "contradiction") -> None:
        # Delete both directions: contradictions are conceptually undirected.
        sql = (
            f"DELETE FROM {self._edges_table} WHERE org_id = %s AND user_id = %s AND kind = %s "
            "AND ((src_id = %s AND dst_id = %s) OR (src_id = %s AND dst_id = %s))"
        )
        params: list[object] = [self.org_id, self.user_id, kind, src_id, dst_id, dst_id, src_id]
        if self._cache_key is not None:
            sql += " AND cache_key = %s"
            params.append(self._cache_key)
        self._conn.execute(sql, params)

    def flip_edge_kind(
        self,
        a_id: str,
        b_id: str,
        *,
        from_kind: str = "contradiction",
        to_kind: str = "contradicted_by",
    ) -> None:
        """Re-label the edge between two facts (undirected, idempotent).

        Used when a contradiction is resolved: the pair stays linked but the edge
        kind changes (``contradiction`` -> ``contradicted_by``), so the resolved
        relationship is preserved and reversible rather than deleted. Drops the
        old-kind row in either direction and writes a single canonical-ordered
        row at the new kind.
        """
        self.remove_edge(a_id, b_id, from_kind)
        src, dst = sorted((a_id, b_id))
        self.add_edge(src, dst, to_kind)

    def all_edges(self, kind: str | None = None) -> list[tuple[str, str, str]]:
        """All (src, dst, kind) edges for the tenant, regardless of fact state."""
        sql = (
            f"SELECT src_id, dst_id, kind FROM {self._edges_table} "
            "WHERE org_id = %s AND user_id = %s"
        )
        params: list[object] = [self.org_id, self.user_id]
        if self._cache_key is not None:
            sql += " AND cache_key = %s"
            params.append(self._cache_key)
        if kind is not None:
            sql += " AND kind = %s"
            params.append(kind)
        rows = self._conn.execute(sql, params).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def edges_touching(self, fact_id: str) -> list[tuple[str, str, str]]:
        """All (src, dst, kind) edges with ``fact_id`` as either endpoint.

        The per-fact counterpart to :meth:`all_edges`: one indexed lookup for a
        single fact's edges instead of scanning the whole tenant. Used by the
        candidate facade's single-fact rival lookup so ``get`` (and every mutation
        that re-reads one candidate) does not pay a full-tenant edge scan."""
        sql = (
            f"SELECT src_id, dst_id, kind FROM {self._edges_table} "
            "WHERE org_id = %s AND user_id = %s AND (src_id = %s OR dst_id = %s)"
        )
        params: list[object] = [self.org_id, self.user_id, fact_id, fact_id]
        if self._cache_key is not None:
            sql += " AND cache_key = %s"
            params.append(self._cache_key)
        rows = self._conn.execute(sql, params).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    # --- surface<->requirement bindings ------------------------------------
    # A typed ``renders`` relation (RENDERS_EDGE) from a requirement fact to a
    # SURFACE fact (a wireframe screen modeled as a fact, see RENDERS_EDGE). These
    # reuse the fact_edges infrastructure directly (no new table): a binding is an
    # edge add, an unbinding an edge remove, and every read is active-only (mirrors
    # ``active_edges``) so a rejected endpoint silently drops from coverage with no
    # stale hook. Surfaces are idempotent on (project, screen_id).
    def _find_surface(self, project: str, screen_id: str) -> Fact | None:
        """The (at most one) surface fact for ``(project, screen_id)`` — any state.

        Idempotency key: scope=project, category="surface", meta->>'screen_id'=screen_id.
        Honors ``cache_key`` like ``all_facts``; newest first if (defensively) more than one.
        """
        sql = (
            "SELECT id, text, source, confidence, scope, category, observation_count, "
            "state, created_at, meta, cluster_id, cluster_label "
            f"FROM {self._facts_table} WHERE org_id = %s AND (shared OR user_id = %s) "
            "AND category = %s AND scope = %s AND meta->>'screen_id' = %s"
        )
        params: list[object] = [self.org_id, self.user_id, SURFACE_CATEGORY, project, screen_id]
        if self._cache_key is not None:
            sql += " AND cache_key = %s"
            params.append(self._cache_key)
        sql += " ORDER BY created_at DESC LIMIT 1"
        rows = self._conn.execute(sql, params).fetchall()
        if not rows:
            return None
        return self._row_to_fact(rows[0])

    def _active_renders_edges(self) -> list[tuple[str, str]]:
        """``(src, dst)`` RENDERS edges where BOTH endpoint facts are ``active``.

        Mirrors ``active_edges`` (the same active-on-both-ends join) with a
        ``kind = RENDERS_EDGE`` filter, so a rejected requirement or surface drops
        its edge from every coverage/read result with no stale hook.
        """
        rows = self._conn.execute(
            f"SELECT e.src_id, e.dst_id FROM {self._edges_table} e "
            f"JOIN {self._facts_table} s ON s.org_id = e.org_id AND s.user_id = e.user_id AND s.id = e.src_id "
            f"JOIN {self._facts_table} d ON d.org_id = e.org_id AND d.user_id = e.user_id AND d.id = e.dst_id "
            "WHERE e.org_id = %s AND (s.shared OR s.user_id = %s) "
            "AND e.kind = %s AND s.state = 'active' AND d.state = 'active'",
            (self.org_id, self.user_id, RENDERS_EDGE),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def ensure_surface(
        self,
        project: str,
        screen_id: str,
        *,
        title: str | None = None,
        file: str | None = None,
        states: list[str] | None = None,
    ) -> str:
        """Idempotently materialize the surface fact for ``(project, screen_id)``.

        If one already exists (``_find_surface``), merge-update its meta (only when
        the merged value actually changed) via ``set_meta`` and return its id; else
        ``_add`` a fresh active surface fact (constructed via a WriteDecision exactly
        like ``record_episode``) and return the new id. At most ONE surface fact per
        ``(project, screen_id)``.
        """
        existing = self._find_surface(project, screen_id)
        if existing is not None:
            current = dict(existing.meta or {})
            merged = dict(current)
            merged["screen_id"] = screen_id
            if title is not None:
                merged["title"] = title
            if file is not None:
                merged["file"] = file
            if states is not None:
                merged["states"] = list(states)
            merged.setdefault("title", None)
            merged.setdefault("file", None)
            merged.setdefault("states", [])
            if merged != current:
                self.set_meta(existing.id, merged)
            return existing.id
        text = title or screen_id
        decision = WriteDecision(text=text, state="active")
        decision.source = None
        decision.scope = project
        decision.category = SURFACE_CATEGORY
        decision.meta = {
            "screen_id": screen_id,
            "title": title,
            "file": file,
            "states": list(states or []),
        }
        decision.embedding = self._embed(text)
        return self._add(decision)  # store-only: a surface is not a semantic learning

    def bind_surface(
        self,
        requirement_fact_id: str,
        screen_id: str,
        project: str,
        *,
        title: str | None = None,
        file: str | None = None,
        states: list[str] | None = None,
    ) -> str:
        """Bind ``requirement_fact_id`` -> the ``(project, screen_id)`` surface (RENDERS).

        Ensures the surface exists, then writes the typed ``renders`` edge
        (src=requirement, dst=surface). Idempotent (``add_edge`` is ON CONFLICT DO
        NOTHING; ``ensure_surface`` is idempotent). Returns the surface id.
        """
        surface_id = self.ensure_surface(
            project, screen_id, title=title, file=file, states=states
        )
        self.add_edge(requirement_fact_id, surface_id, RENDERS_EDGE)
        return surface_id

    def unbind_surface(self, requirement_fact_id: str, screen_id: str, project: str) -> None:
        """Drop the RENDERS edge from ``requirement_fact_id`` to the surface (idempotent).

        Looks up the surface for ``(project, screen_id)``; if found, removes the
        ``renders`` edge. The surface fact itself is left intact. No-op if absent.
        """
        surface = self._find_surface(project, screen_id)
        if surface is not None:
            self.remove_edge(requirement_fact_id, surface.id, RENDERS_EDGE)

    def requirements_for_surface(self, project: str, screen_id: str) -> list[Fact]:
        """PRIMARY query: active requirement facts that RENDER ``(project, screen_id)``.

        Joins ``fact_edges`` (kind=RENDERS_EDGE, dst=surface) to the source facts and
        returns the ``active`` ones, newest first. Empty when the surface is unknown.
        """
        surface = self._find_surface(project, screen_id)
        if surface is None:
            return []
        sql = (
            "SELECT r.id, r.text, r.source, r.confidence, r.scope, r.category, "
            "r.observation_count, r.state, r.created_at, r.meta, r.cluster_id, r.cluster_label "
            f"FROM {self._edges_table} e "
            f"JOIN {self._facts_table} r ON r.org_id = e.org_id AND r.user_id = e.user_id AND r.id = e.src_id "
            "WHERE e.org_id = %s AND (r.shared OR r.user_id = %s) "
            "AND e.kind = %s AND e.dst_id = %s AND r.state = 'active'"
        )
        params: list[object] = [self.org_id, self.user_id, RENDERS_EDGE, surface.id]
        if self._cache_key is not None:
            sql += " AND e.cache_key = %s"
            params.append(self._cache_key)
        sql += " ORDER BY r.created_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def surfaces_for_requirement(self, requirement_fact_id: str) -> list[Fact]:
        """Active surface facts governed by ``requirement_fact_id`` (newest first).

        The dst side of the RENDERS edges out of the requirement, restricted to
        ``active`` surface facts.
        """
        sql = (
            "SELECT d.id, d.text, d.source, d.confidence, d.scope, d.category, "
            "d.observation_count, d.state, d.created_at, d.meta, d.cluster_id, d.cluster_label "
            f"FROM {self._edges_table} e "
            f"JOIN {self._facts_table} d ON d.org_id = e.org_id AND d.user_id = e.user_id AND d.id = e.dst_id "
            "WHERE e.org_id = %s AND (d.shared OR d.user_id = %s) "
            "AND e.kind = %s AND e.src_id = %s AND d.state = 'active' AND d.category = %s"
        )
        params: list[object] = [
            self.org_id, self.user_id, RENDERS_EDGE, requirement_fact_id, SURFACE_CATEGORY
        ]
        if self._cache_key is not None:
            sql += " AND e.cache_key = %s"
            params.append(self._cache_key)
        sql += " ORDER BY d.created_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def list_surface_bindings(self, project: str) -> list[dict]:
        """Every RENDERS edge whose dst surface fact has ``scope = project`` (any state).

        Returns ``{"requirementId","surfaceId","screenId"}`` per edge; ``screenId``
        comes from the surface fact's ``meta->>'screen_id'``.
        """
        sql = (
            "SELECT e.src_id, e.dst_id, d.meta->>'screen_id' "
            f"FROM {self._edges_table} e "
            f"JOIN {self._facts_table} d ON d.org_id = e.org_id AND d.user_id = e.user_id AND d.id = e.dst_id "
            "WHERE e.org_id = %s AND (d.shared OR d.user_id = %s) "
            "AND e.kind = %s AND d.category = %s AND d.scope = %s"
        )
        params: list[object] = [
            self.org_id, self.user_id, RENDERS_EDGE, SURFACE_CATEGORY, project
        ]
        if self._cache_key is not None:
            sql += " AND e.cache_key = %s"
            params.append(self._cache_key)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {"requirementId": r[0], "surfaceId": r[1], "screenId": r[2]} for r in rows
        ]

    def surface_coverage(self, project: str, *, scope: str | None = None) -> dict:
        """Bidirectional completeness gate for ``project``.

        ``uncoveredSurfaces`` = active surface facts (scope=project) with ZERO active
        RENDERS edge as dst. ``uncoveredRequirements`` = active requirement facts with
        ZERO active RENDERS edge as src, optionally filtered to ``meta->>'scope' =
        scope`` (e.g. scope="mvp"). Active-only on both ends (``_active_renders_edges``),
        so a rejected endpoint re-opens coverage with no stale hook.
        """
        active = self._active_renders_edges()
        covered_dst = {dst for _src, dst in active}
        covered_src = {src for src, _dst in active}
        uncovered_surfaces: list[Fact] = []
        uncovered_requirements: list[Fact] = []
        for fact in self.all_facts(state="active"):
            if fact.category == SURFACE_CATEGORY and fact.scope == project:
                if fact.id not in covered_dst:
                    uncovered_surfaces.append(fact)
            elif fact.category == REQUIREMENT_CATEGORY:
                if scope is not None and (fact.meta or {}).get("scope") != scope:
                    continue
                if fact.id not in covered_src:
                    uncovered_requirements.append(fact)
        return {
            "uncoveredSurfaces": uncovered_surfaces,
            "uncoveredRequirements": uncovered_requirements,
        }

    @staticmethod
    def _completeness_reasons(
        success_count: int, last_outcome: str | None, is_stale: bool
    ) -> list[str]:
        """Why an active requirement is NOT verified-complete, derived from signals.

        A requirement is incomplete when ANY holds (the model, all derived — never a
        self-set 'done' flag):
          * ``never-built`` — no successful outcome yet (``success_count == 0``);
          * ``regressed``   — it succeeded before but its *latest* outcome was a
            failure (``last_outcome == 'failed'`` with at least one prior success);
          * ``stale``       — a fact it derives from was invalidated (carries a
            ``STALE_DERIVED_EDGE``), so it needs rework.
        Returns every applicable reason, primary first (``never-built`` > ``regressed``
        > ``stale``); an empty list means COMPLETE (latest outcome succeeded, not
        stale). The never-built / regressed branches are exclusive (a requirement with
        zero successes is never-built, not regressed, even if its last outcome failed),
        so the primary reason partitions the incomplete set cleanly for the summary.
        """
        reasons: list[str] = []
        if success_count == 0:
            reasons.append("never-built")
        elif last_outcome == "failed":
            reasons.append("regressed")
        if is_stale:
            reasons.append("stale")
        return reasons

    def _classify_project_requirements(self, project: str) -> list[dict]:
        """Every active ``category='requirement'`` fact in ``prd-<project>``, each
        annotated with its completeness reasons (empty list == complete), newest first.

        The single read+classify pass both ``incomplete_requirements`` and
        ``completeness_summary`` share — one requirements query plus one stale-edge
        query, so the two public methods stay consistent and cheap. Project scoping
        mirrors how the factory seeds a plan (requirement facts ingested with
        ``source='prd-<project>'``) and respects the same tenancy / sharing visibility,
        active-only filtering, and cache binding as the rest of the read surface.

        Each entry::

            {"fact": Fact, "reason": str | None, "reasons": list[str],
             "success_count": int, "failure_count": int, "last_outcome": str | None}

        ``reasons`` is every applicable cause (primary first); ``reason`` is the primary
        cause, or ``None`` when the requirement is complete. See ``_completeness_reasons``.
        """
        source = f"prd-{project}"
        cache = ""
        params: list[object] = [
            self.org_id, self.user_id, REQUIREMENT_CATEGORY, source
        ]
        if self._cache_key is not None:
            cache = " AND cache_key = %s"
            params.append(self._cache_key)
        rows = self._conn.execute(
            "SELECT id, text, source, confidence, scope, category, observation_count, "
            "state, created_at, meta, cluster_id, cluster_label, "
            "success_count, failure_count, last_outcome "
            f"FROM {self._facts_table} "
            "WHERE org_id = %s AND (shared OR user_id = %s) AND state = 'active' "
            f"AND category = %s AND source = %s{cache} "
            "ORDER BY created_at DESC",
            params,
        ).fetchall()
        stale_ids = self._stale_flagged_ids()
        out: list[dict] = []
        for r in rows:
            fact = self._row_to_fact(r[:12])
            success_count = r[12] or 0
            failure_count = r[13] or 0
            last_outcome = r[14]
            fact.success_count = success_count
            fact.failure_count = failure_count
            fact.last_outcome = last_outcome
            reasons = self._completeness_reasons(
                success_count, last_outcome, fact.id in stale_ids
            )
            out.append(
                {
                    "fact": fact,
                    "reason": reasons[0] if reasons else None,
                    "reasons": reasons,
                    "success_count": success_count,
                    "failure_count": failure_count,
                    "last_outcome": last_outcome,
                }
            )
        return out

    def incomplete_requirements(self, project: str) -> list[dict]:
        """Active requirement facts in ``prd-<project>`` that are NOT verified-complete.

        The primary query the agent-factory loop calls to pick "the next unbuilt
        requirement". Completeness is DERIVED from verification + staleness signals
        only (``record_outcome`` results + ``_flag_stale_dependents`` flags) — there is
        no agent-settable completeness column. A requirement is incomplete when it has
        never succeeded (never-built), most-recently failed after a prior success
        (regressed — the bug/ticket path), or is flagged stale (a dependency changed).

        Returns one entry per incomplete requirement, newest first::

            {"fact": Fact, "reason": str, "reasons": list[str],
             "success_count": int, "failure_count": int, "last_outcome": str | None}

        ``reason`` is the primary cause; ``reasons`` lists every cause that applies.
        Complete requirements (latest outcome succeeded, not stale) are omitted; so are
        rejected/superseded ones (active-only). See ``_completeness_reasons``.
        """
        return [
            item
            for item in self._classify_project_requirements(project)
            if item["reasons"]
        ]

    def completeness_summary(self, project: str) -> dict:
        """Done-of-definition counts for ``prd-<project>``'s active requirements.

        ``{total_active_requirements, complete, incomplete, breakdown}`` where
        ``breakdown`` counts incomplete requirements by primary reason
        (``never_built`` / ``stale`` / ``regressed``) and sums to ``incomplete`` (the
        primary reason partitions the set — see ``_completeness_reasons``). Derived
        from the same verification + staleness signals as ``incomplete_requirements``.
        """
        classified = self._classify_project_requirements(project)
        incomplete = [item for item in classified if item["reasons"]]
        breakdown = {"never_built": 0, "stale": 0, "regressed": 0}
        for item in incomplete:
            breakdown[item["reason"].replace("-", "_")] += 1
        total = len(classified)
        return {
            "total_active_requirements": total,
            "complete": total - len(incomplete),
            "incomplete": len(incomplete),
            "breakdown": breakdown,
        }

    def wipe_cache(self) -> int:
        """Delete every cached fact under this graph's bound ``cache_key``.

        Cache tables only (the graph must have been built with a ``cache_key``).
        Edges cascade via the FK. Returns the number of facts removed.
        """
        if self._cache_key is None:
            raise ValueError("wipe_cache requires a cache_key-bound graph")
        cur = self._conn.execute(
            f"DELETE FROM {self._facts_table} WHERE org_id = %s AND user_id = %s AND cache_key = %s",
            (self.org_id, self.user_id, self._cache_key),
        )
        return cur.rowcount

    # --- cache save/load (snapshots + eval datasets) -----------------------
    def _require_live(self, op: str) -> None:
        if self._facts_table != "facts" or self._cache_key is not None:
            raise ValueError(f"{op} must be called on the live (facts) graph")

    def save_cache(self, cache_key: str) -> int:
        """Snapshot the live graph into the cache under ``cache_key`` (upsert).

        Pure SQL copy — embeddings, ids, states and meta are preserved verbatim,
        so a later ``load_cache`` restores losslessly with no re-embedding. Any
        existing rows under ``cache_key`` are replaced. Returns facts copied.
        """
        self._require_live("save_cache")
        tenant = (self.org_id, self.user_id, cache_key)
        # Replace any prior state under this key (edges first for the FK).
        self._conn.execute(
            "DELETE FROM cached_fact_edges WHERE org_id=%s AND user_id=%s AND cache_key=%s", tenant
        )
        self._conn.execute(
            "DELETE FROM cached_claims WHERE org_id=%s AND user_id=%s AND cache_key=%s", tenant
        )
        self._conn.execute(
            "DELETE FROM cached_facts WHERE org_id=%s AND user_id=%s AND cache_key=%s", tenant
        )
        self._conn.execute(
            f"INSERT INTO cached_facts ({_FACT_COPY_COLS}, cache_key) "
            f"SELECT {_FACT_COPY_COLS}, %s FROM facts WHERE org_id=%s AND user_id=%s",
            (cache_key, self.org_id, self.user_id),
        )
        self._conn.execute(
            "INSERT INTO cached_fact_edges (org_id, user_id, cache_key, src_id, dst_id, kind) "
            "SELECT org_id, user_id, %s, src_id, dst_id, kind FROM fact_edges "
            "WHERE org_id=%s AND user_id=%s",
            (cache_key, self.org_id, self.user_id),
        )
        self._conn.execute(
            f"INSERT INTO cached_claims ({_CLAIM_COPY_COLS}, cache_key) "
            f"SELECT {_CLAIM_COPY_COLS}, %s FROM claims WHERE org_id=%s AND user_id=%s",
            (cache_key, self.org_id, self.user_id),
        )
        row = self._conn.execute(
            "SELECT count(*) FROM cached_facts WHERE org_id=%s AND user_id=%s AND cache_key=%s",
            tenant,
        ).fetchone()
        return int(row[0]) if row else 0

    def load_cache(self, cache_key: str) -> int:
        """Replace the live graph with one cached state (see ``load_caches``)."""
        return self.load_caches([cache_key])

    def load_caches(self, cache_keys: list[str]) -> int:
        """Replace the live graph with the union of the given cached states.

        Full truncate + insert for this tenant: the live ``facts``/``fact_edges``
        rows are deleted and replaced by the rows of every entry in
        ``cache_keys`` (embeddings and all). A snapshot load passes one key; an
        eval folder load passes its cases' ``eval:<id>`` keys. Returns facts loaded.
        """
        self._require_live("load_caches")
        keys = list(cache_keys)
        # Truncate the live tenant graph (edges first for the FK), then refill.
        self._conn.execute(
            "DELETE FROM fact_edges WHERE org_id=%s AND user_id=%s", (self.org_id, self.user_id)
        )
        self._conn.execute(
            "DELETE FROM claims WHERE org_id=%s AND user_id=%s", (self.org_id, self.user_id)
        )
        self._conn.execute(
            "DELETE FROM facts WHERE org_id=%s AND user_id=%s", (self.org_id, self.user_id)
        )
        if keys:
            self._conn.execute(
                f"INSERT INTO facts ({_FACT_COPY_COLS}) "
                f"SELECT {_FACT_COPY_COLS} FROM cached_facts "
                "WHERE org_id=%s AND user_id=%s AND cache_key = ANY(%s)",
                (self.org_id, self.user_id, keys),
            )
            self._conn.execute(
                "INSERT INTO fact_edges (org_id, user_id, src_id, dst_id, kind) "
                "SELECT org_id, user_id, src_id, dst_id, kind FROM cached_fact_edges "
                "WHERE org_id=%s AND user_id=%s AND cache_key = ANY(%s)",
                (self.org_id, self.user_id, keys),
            )
            self._conn.execute(
                f"INSERT INTO claims ({_CLAIM_COPY_COLS}) "
                f"SELECT {_CLAIM_COPY_COLS} FROM cached_claims "
                "WHERE org_id=%s AND user_id=%s AND cache_key = ANY(%s)",
                (self.org_id, self.user_id, keys),
            )
        row = self._conn.execute(
            "SELECT count(*) FROM facts WHERE org_id=%s AND user_id=%s", (self.org_id, self.user_id)
        ).fetchone()
        return int(row[0]) if row else 0

    def merge_caches_into_live(self, cache_keys: list[str]) -> int:
        """Additively upsert the given cached states into the live graph.

        Unlike ``load_caches`` (which truncates the whole live graph first), this
        keeps every other live fact: for each fact in the selected cache entries
        it deletes any live fact with the same id (so re-adding an eval replaces
        its own nodes) and then inserts the cached rows + edges. Returns facts
        inserted.
        """
        self._require_live("merge_caches_into_live")
        keys = list(cache_keys)
        if not keys:
            return 0
        tenant_keys = (self.org_id, self.user_id, keys)
        # Ids about to be (re)inserted from the cache.
        id_subq = (
            "SELECT id FROM cached_facts "
            "WHERE org_id=%s AND user_id=%s AND cache_key = ANY(%s)"
        )
        # Drop any existing live copies of those ids (edges first for the FK).
        self._conn.execute(
            "DELETE FROM fact_edges WHERE org_id=%s AND user_id=%s "
            f"AND (src_id IN ({id_subq}) OR dst_id IN ({id_subq}))",
            (self.org_id, self.user_id, *tenant_keys, *tenant_keys),
        )
        self._conn.execute(
            f"DELETE FROM facts WHERE org_id=%s AND user_id=%s AND id IN ({id_subq})",
            (self.org_id, self.user_id, *tenant_keys),
        )
        self._conn.execute(
            f"INSERT INTO facts ({_FACT_COPY_COLS}) "
            f"SELECT {_FACT_COPY_COLS} FROM cached_facts "
            "WHERE org_id=%s AND user_id=%s AND cache_key = ANY(%s)",
            tenant_keys,
        )
        self._conn.execute(
            "INSERT INTO fact_edges (org_id, user_id, src_id, dst_id, kind) "
            "SELECT org_id, user_id, src_id, dst_id, kind FROM cached_fact_edges "
            "WHERE org_id=%s AND user_id=%s AND cache_key = ANY(%s)",
            tenant_keys,
        )
        # Claims for the dropped fact ids cascaded on the facts delete above; refill
        # them from the cache (a re-added eval replaces its own claim rows).
        self._conn.execute(
            f"INSERT INTO claims ({_CLAIM_COPY_COLS}) "
            f"SELECT {_CLAIM_COPY_COLS} FROM cached_claims "
            "WHERE org_id=%s AND user_id=%s AND cache_key = ANY(%s)",
            tenant_keys,
        )
        row = self._conn.execute(
            "SELECT count(*) FROM cached_facts WHERE org_id=%s AND user_id=%s AND cache_key = ANY(%s)",
            tenant_keys,
        ).fetchone()
        return int(row[0]) if row else 0

    def list_caches(self, prefix: str) -> list[dict]:
        """List saved cache entries whose key starts with ``prefix``.

        Returns ``[{"key", "count", "created_at"}]`` newest-first. Use prefix
        ``"snapshot:"`` for snapshots or ``"eval:"`` for cached eval cases.
        """
        self._require_live("list_caches")
        rows = self._conn.execute(
            "SELECT cache_key, count(*), max(created_at) FROM cached_facts "
            "WHERE org_id=%s AND user_id=%s AND cache_key LIKE %s "
            "GROUP BY cache_key ORDER BY max(created_at) DESC",
            (self.org_id, self.user_id, prefix + "%"),
        ).fetchall()
        return [
            {
                "key": r[0],
                "count": int(r[1]),
                "created_at": r[2].isoformat() if hasattr(r[2], "isoformat") else str(r[2]),
            }
            for r in rows
        ]

    def delete_cache(self, cache_key: str) -> int:
        """Delete a saved cache entry (edges cascade). Returns facts removed."""
        self._require_live("delete_cache")
        cur = self._conn.execute(
            "DELETE FROM cached_facts WHERE org_id=%s AND user_id=%s AND cache_key=%s",
            (self.org_id, self.user_id, cache_key),
        )
        return cur.rowcount

    def cache_count(self, cache_key: str) -> int:
        """How many facts are cached under ``cache_key`` (0 == not cached)."""
        self._require_live("cache_count")
        row = self._conn.execute(
            "SELECT count(*) FROM cached_facts WHERE org_id=%s AND user_id=%s AND cache_key=%s",
            (self.org_id, self.user_id, cache_key),
        ).fetchone()
        return int(row[0]) if row else 0

    # --- shared per-write recall (feeds the write steps) -------------------
    def _recall(self, decision: WriteDecision) -> None:
        """Embed the incoming text once and run the single candidate pass.

        Fills ``decision.embedding`` and ``decision.candidates`` (existing facts
        scoring >= ``recall_floor``, best first, capped at ``recall_k``). Dedup and
        conflict both see pending facts, so this searches all states.
        """
        decision.embedding = self._embed(decision.text)
        # H2/H4 write-side: a semantic write must never recall an episode as a
        # candidate (so it is never merged-with or contradiction-flagged against a
        # decision log). Episodes run no recall of their own (store-only lane).
        hits = self._search_vec(
            _fit(decision.embedding), top_k=self.recall_k, state=None,
            exclude_categories=[EPISODIC_CATEGORY],
        )
        decision.candidates = [h for h in hits if h.score >= self.recall_floor]

    def _recall_semantic(self, decision: WriteDecision) -> None:
        """Fill ``decision.semantic_candidates`` via a wider, lower-floor recall.

        Reuses the already-computed ``decision.embedding`` (one embedding per write
        still holds). Returns existing facts scoring >= ``semantic_recall_floor``
        (well below the dedup/conflict floor), capped at ``semantic_recall_k``, so
        the semantic LLM judge can see paraphrase contradictions the narrow pass
        drops. Searches all states, like ``_recall``.
        """
        if decision.embedding is None:
            decision.embedding = self._embed(decision.text)
        hits = self._search_vec(
            _fit(decision.embedding), top_k=self.semantic_recall_k, state=None,
            exclude_categories=[EPISODIC_CATEGORY],  # never recall an episode (see _recall)
        )
        decision.semantic_candidates = [
            h for h in hits if h.score >= self.semantic_recall_floor
        ]

    def _recall_claims(self, decision: WriteDecision) -> None:
        """Fill ``decision.claim_candidates`` via the functional (subject, attribute) slot.

        For each functional claim on the incoming write, find existing facts that
        hold a functional claim on the same normalized slot (index ``claims_slot``).
        Only functional slots are considered — multi-valued attributes never conflict.
        """
        slots = {c.slot for c in decision.claims if c.functional}
        if not slots:
            return
        subjects = [s for s, _ in slots]
        attributes = [a for _, a in slots]
        sql = (
            "SELECT c.subject, c.attribute, c.value, f.id, f.text, f.state "
            f"FROM {self._claims_table} c "
            f"JOIN {self._facts_table} f ON f.org_id=c.org_id AND f.user_id=c.user_id "
            "AND f.id=c.fact_id "
            "WHERE c.org_id=%s AND c.user_id=%s AND c.functional "
            "AND (c.subject, c.attribute) IN (SELECT unnest(%s::text[]), unnest(%s::text[]))"
        )
        params: list[object] = [self.org_id, self.user_id, subjects, attributes]
        if self._cache_key is not None:
            sql += " AND c.cache_key=%s AND f.cache_key=%s"
            params.extend([self._cache_key, self._cache_key])
        rows = self._conn.execute(sql, params).fetchall()
        decision.claim_candidates = [
            ClaimHit(
                fact=SearchHit(fact=Fact(id=r[3], text=r[4], state=r[5]), score=1.0),
                subject=r[0],
                attribute=r[1],
                value=r[2],
            )
            for r in rows
        ]

    # --- internals ----------------------------------------------------------
    def _embed(self, text: str) -> list[float]:
        with tracing.llm_span("embed", kind="EMBEDDING", input_value=text) as span:
            vec = self.embedder.embed_one(text)
            tracing.record_output(span, output=f"<{len(vec)}-dim vector>")
        return vec

    def _recent(self, limit: int) -> list[Fact]:
        # Backs the no-context ``read`` path, so it surfaces only retrievable
        # ("active") facts, matching ``search``'s gating.
        rows = self._conn.execute(
            "SELECT id, text, source, confidence, scope, category, observation_count, state "
            f"FROM {self._facts_table} WHERE org_id = %s AND (shared OR user_id = %s) "
            "AND state = 'active' "
            "ORDER BY created_at DESC LIMIT %s",
            (self.org_id, self.user_id, limit),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def _add(self, decision: WriteDecision) -> str:
        # Human-approved adds enter at full credibility (confidence default 1.0).
        embedding = _fit(decision.embedding)  # reuse the vector from _recall
        fact_id = uuid.uuid4().hex
        meta = getattr(decision, "meta", None) or {}
        # Bi-temporal validity: a new fact becomes valid now and stays valid
        # (invalid_at NULL) until something supersedes it. Callers may backdate
        # world-time validity by supplying meta["valid_at"] (ISO string or a
        # datetime); fall back to SQL now() otherwise.
        valid_at = meta.get("valid_at")
        if valid_at is None:
            valid_at = datetime.now(timezone.utc)
        # cache_key only exists on cached_facts; meta exists on both tables.
        cols = ["id", "org_id", "user_id", "text", "source", "confidence",
                "scope", "category", "state", "embedding",
                "valid_at", "invalid_at", "meta"]
        vals: list[object] = [
            fact_id,
            self.org_id,
            self.user_id,
            decision.text,
            getattr(decision, "source", None),
            1.0,
            getattr(decision, "scope", None),
            getattr(decision, "category", None),
            decision.state,
            embedding,
            valid_at,
            None,
            json.dumps(meta),
        ]
        if self._cache_key is not None:
            cols.append("cache_key")
            vals.append(self._cache_key)
        placeholders = ", ".join(["%s"] * len(vals))
        self._conn.execute(
            f"INSERT INTO {self._facts_table} ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        self._persist_claims(fact_id, decision.claims)
        return fact_id

    def _merge(self, decision: WriteDecision) -> None:
        # Near-/exact-dup: bump the existing fact's evidence count, keep text.
        self._conn.execute(
            f"UPDATE {self._facts_table} SET observation_count = observation_count + 1, "
            "confidence = LEAST(1.0, COALESCE(confidence, 1.0) + 0.05) "
            "WHERE org_id = %s AND user_id = %s AND id = %s",
            (self.org_id, self.user_id, decision.update_target_id),
        )

    def _augment(self, decision: WriteDecision) -> None:
        """Mem0 UPDATE/merge: rewrite the target fact's text to the merged survivor.

        Keeps a single fact: the existing fact (``update_target_id``) absorbs the
        incoming note's content (``augment_text``), is re-embedded so retrieval
        tracks the merged text, and bumps observation_count/confidence. The
        incoming note is not inserted as a separate row. Claims are left as-is on
        the surviving fact (the merged text is additive, not a slot conflict).
        """
        merged = (decision.augment_text or decision.text).strip()
        self._conn.execute(
            f"UPDATE {self._facts_table} SET text = %s, embedding = %s, "
            "observation_count = observation_count + 1, "
            "confidence = LEAST(1.0, COALESCE(confidence, 1.0) + 0.05) "
            "WHERE org_id = %s AND user_id = %s AND id = %s",
            (merged, _fit(self._embed(merged)), self.org_id, self.user_id, decision.update_target_id),
        )

    def _overwrite(self, decision: WriteDecision) -> str:
        """Non-destructive resolution of an approved contradiction (FR-003/FR-005).

        The approved fact is added fresh (a normal ``_add`` at ``decision.state``,
        which also persists its extracted claims); every contradicting fact — the
        nearest (``update_target_id``) and the rest (``supersede_ids``) — is treated
        uniformly as a loser: set ``rejected`` with its ``text``/``embedding`` left
        untouched, and linked to the new fact with a ``contradicted_by`` edge. No
        loser's content is destroyed (SC-001) and, since every loser is rejected, no
        contradicting pair is left both ``active`` (SC-002). Only the direct losers
        of *this* write change state — no cascade (FR-009). Returns the new fact's id.
        """
        fact_id = self._add(decision)
        losers = [decision.update_target_id, *decision.supersede_ids]
        for loser_id in losers:
            if not loser_id or loser_id == fact_id:
                continue
            # Bi-temporal invalidation (additive to the existing `rejected`
            # state): the loser stopped being true when the winner became true,
            # so close its validity window at the winner's valid_at (falling
            # back to now()). The row and its text are kept for point-in-time
            # recall — an `as_of` before the winner still recovers the loser.
            self._conn.execute(
                f"UPDATE {self._facts_table} AS loser SET state = 'rejected', "
                "invalid_at = COALESCE("
                f"  (SELECT winner.valid_at FROM {self._facts_table} AS winner "
                "    WHERE winner.org_id = loser.org_id "
                "      AND winner.user_id = loser.user_id AND winner.id = %s), "
                "  now()) "
                "WHERE loser.org_id = %s AND loser.user_id = %s AND loser.id = %s",
                (fact_id, self.org_id, self.user_id, loser_id),
            )
            src, dst = sorted((fact_id, loser_id))
            self.add_edge(src, dst, "contradicted_by")
            self._flag_stale_dependents(loser_id)  # H5: propagate to derived learnings
        return fact_id
