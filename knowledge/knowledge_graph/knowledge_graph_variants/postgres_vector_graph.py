"""Persistent vector store of facts, backed by the RDS ``facts`` table.

The durable sibling of :class:`~knowledge.knowledge_graph.knowledge_graph_variants.vector_graph.VectorGraph`:
same ``KnowledgeGraph``/``SearchableGraph`` contract and the same write-policy
pipeline (redact -> dedup -> conflict), but every fact lives in Postgres with a
pgvector embedding. One graph is bound to exactly one partition: either a user's
private **working memory** (table ``facts``, keyed ``(org_id, user_id)``) or one
org-shared **snapshot** (table ``snapshots``, keyed ``(org_id, space, snapshot)``)
when constructed with ``facts_table='snapshots'`` + ``space``/``snapshot``.
Retrieval is a pgvector cosine search; the read predicate is exactly the graph's
partition key — ``org_id=%s AND user_id=%s`` (working memory) or
``org_id=%s AND space=%s AND snapshot=%s`` (snapshot) — with no ``shared``
disjunction (org-sharing is enforced one layer up by org membership).

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
# identifiers), so they must NEVER be user-controlled. A graph binds to one of two
# partition families — working memory (``facts``) or org-shared snapshots
# (``snapshots``) — and each carries its own edge/claim satellites. The satellite
# names are DERIVED from the facts table (never caller-controlled), so only the
# facts table needs the allowlist guard.
_SATELLITE_TABLES = {
    "facts": ("fact_edges", "claims"),
    "snapshots": ("snapshot_edges", "snapshot_claims"),
}
_ALLOWED_FACTS_TABLES = frozenset(_SATELLITE_TABLES)

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

# Coverage "check" facts (agent-factory coverage spine). A free-form category like
# any other (no schema change): a check is a fact with category="check" carrying its
# applicability/severity in ``meta`` (e.g. meta.scope="planning"|"validation",
# meta.applies_to, meta.angle, meta.severity). The factory writes them via the RAW
# insert path (``/candidates`` -> ``_add``) so distinct checks ACCUMULATE rather than
# being deduped/merged by the write policy. Read back EXHAUSTIVELY (not top-k) via
# ``facts_by`` / ``checks_for_surface`` so a completeness gate never silently drops one.
CHECK_CATEGORY = "check"


class SnapshotKindError(ValueError):
    """A fact would violate the KIND its destination snapshot allows (the kind is derived from the
    snapshot NAME). Raised at every insert-into-``snapshots`` path so validation checks can never
    co-mingle with a ``prd-<project>`` plan (or vice versa) — the invariant that makes the "11
    validation checks embedded in a prd snapshot" failure impossible. Subclasses ``ValueError`` so
    the candidate write routes (which already map ``ValueError`` -> HTTP 400) surface it unchanged.
    """


# A snapshot's KIND (derived from its NAME) restricts the facts it may hold. This ONE derivation +
# spec feeds BOTH the per-row Python guard (``_add``) and the SQL violator probe (the bulk save/copy
# paths), so there is exactly one source of truth for the rule:
#   * ``prd-<project>`` (name starts ``prd-``) -> a PLAN: NO ``category="check"`` facts.
#   * ``building-validation``                  -> ONLY checks with scope ``"validation"``.
#   * ``planning-validation``                  -> ONLY checks with scope ``"planning"``.
#   * any other name (evals, demos, ad-hoc)    -> UNCONSTRAINED (kind None).
# A check stores its scope in ``meta.scope`` (the top-level ``scope`` column is typically NULL for
# checks — see the CHECK_CATEGORY note above), so the invariant reads scope as
# ``COALESCE(meta->>'scope', scope)``, mirroring ``_ticket_state._scope_of``.
def _snapshot_kind(snapshot: str | None) -> str | None:
    """Derive a snapshot's KIND from its NAME (None == unconstrained)."""
    if not snapshot:
        return None
    if snapshot.startswith("prd-"):
        return "plan"
    if snapshot == "building-validation":
        return "building-validation"
    if snapshot == "planning-validation":
        return "planning-validation"
    return None


def _row_allowed(kind: str | None, category: str | None, scope: str | None) -> bool:
    """True iff a row with ``category``/``scope`` is allowed into a snapshot of ``kind``."""
    if kind is None:
        return True
    is_check = category == CHECK_CATEGORY
    if kind == "plan":
        return not is_check
    if kind == "building-validation":
        return is_check and scope == "validation"
    if kind == "planning-validation":
        return is_check and scope == "planning"
    return True


def _snapshot_violator_sql(kind: str | None) -> str | None:
    """A SQL boolean matching a FORBIDDEN row over the ``category``/``scope``/``meta`` columns (scope
    resolved as ``COALESCE(meta->>'scope', scope)``). None when ``kind`` is unconstrained."""
    if kind == "plan":
        return "category = 'check'"
    if kind == "building-validation":
        return ("(category IS DISTINCT FROM 'check' "
                "OR COALESCE(meta->>'scope', scope) IS DISTINCT FROM 'validation')")
    if kind == "planning-validation":
        return ("(category IS DISTINCT FROM 'check' "
                "OR COALESCE(meta->>'scope', scope) IS DISTINCT FROM 'planning')")
    return None


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

# Working-memory (`facts`) copy columns — for load (snapshot -> facts) and the
# cross-tenant live copy. Carries `user_id` (the partition owner); NO `shared`
# (dropped in the tenancy redesign). `space`/`snapshot` never appear on facts.
_FACT_COPY_COLS = (
    "id, org_id, user_id, text, source, confidence, scope, category, "
    "observation_count, state, embedding, cluster_id, cluster_label, "
    "valid_at, invalid_at, meta, created_at"
)

# Snapshot (`snapshots`) copy columns — for save (facts -> snapshot) and cross-org
# snapshot copy. NO `user_id`, NO `shared`, and (like the old cache) it OMITS the
# outcome-trust columns. `space`/`snapshot` are stamped per copy, so they are not
# listed here.
_SNAPSHOT_COPY_COLS = (
    "id, org_id, text, source, confidence, scope, category, "
    "observation_count, state, embedding, cluster_id, cluster_label, "
    "valid_at, invalid_at, meta, created_at"
)

# Working-memory (`claims`) copy columns. Snapshot side (`snapshot_claims`) drops
# `user_id` and stamps `space`/`snapshot` per copy — see `_SNAPSHOT_CLAIM_COPY_COLS`.
_CLAIM_COPY_COLS = (
    "org_id, user_id, fact_id, seq, subject, attribute, value, functional, created_at"
)
_SNAPSHOT_CLAIM_COPY_COLS = (
    "org_id, fact_id, seq, subject, attribute, value, functional, created_at"
)


def _restamp_select(cols: str) -> str:
    """``cols`` as a SELECT list with ``org_id``/``user_id`` swapped for ``%s``.

    Cross-tenant / cross-partition copies reuse the canonical copy column lists but
    re-stamp the tenant keys to the DESTINATION values instead of carrying the
    source's. The placeholders appear in column order, so callers bind the swapped
    keys first (followed by any trailing literals like ``space``/``snapshot``). The
    snapshot list has no ``user_id``, so only its ``org_id`` becomes a placeholder.
    """
    return ", ".join(
        "%s" if c.strip() in ("org_id", "user_id") else c.strip()
        for c in cols.split(",")
    )


# SELECT projections for the copy column lists with the tenant keys re-stamped to
# bound placeholders — see ``_restamp_select`` and the copy/load methods.
#   _FACT_RESTAMP_SELECT     -> id, %s(org), %s(user), <rest>   (2 placeholders)
#   _SNAPSHOT_RESTAMP_SELECT -> id, %s(org), <rest>             (1 placeholder)
_FACT_RESTAMP_SELECT = _restamp_select(_FACT_COPY_COLS)
_SNAPSHOT_RESTAMP_SELECT = _restamp_select(_SNAPSHOT_COPY_COLS)
_CLAIM_RESTAMP_SELECT = _restamp_select(_CLAIM_COPY_COLS)
_SNAPSHOT_CLAIM_RESTAMP_SELECT = _restamp_select(_SNAPSHOT_CLAIM_COPY_COLS)

# Multi-agent build-loop ticket lease (agent factory). A requirement fact carries
# its build lifecycle + lease entirely on ``meta`` (no new table): ``build_state``
# is ``incomplete`` -> ``in_progress`` -> ``finished``; a LIVE claim adds
# ``claim_owner`` (the holding agent/session), ``claim_at`` / ``claim_heartbeat_at``
# (server-clock epoch seconds) and ``claim_lease_ttl`` (seconds). A claim is a LEASE,
# not a lock: a lease whose ``claim_heartbeat_at`` is older than ``claim_lease_ttl``
# is STALE and may be reclaimed by any owner (a dead/stalled agent never dangles).
DEFAULT_LEASE_TTL_SECONDS = 1800
_LEASE_META_KEYS = (
    "claim_owner",
    "claim_at",
    "claim_heartbeat_at",
    "claim_lease_ttl",
)

# Live, agent-owned ``meta`` keys a build worker writes onto a requirement fact
# AFTER a snapshot was saved: the ticket's build lifecycle, its lease, the run it
# belongs to, and the verification contract (pinned checks / required validations /
# block reason). A snapshot reload rebuilds the working graph from the snapshot
# baseline, which predates these — so it must MERGE-PRESERVE them (never revert the
# fact to the baseline), else it silently discards in-flight build state. See
# ``_live_reload_state`` / ``_reapply_agent_meta``.
_LIVE_AGENT_META_KEYS = (
    "build_state",
    *_LEASE_META_KEYS,
    "run_owner",
    "run_at",
    "run_scope",
    "required_validations",
    "pinned_checks",
    "block_reason",
)


class LeaseConflict(Exception):
    """A claim/heartbeat lost to (or was blocked by) a different live lease.

    Carries the current ``owner`` and the ``remaining`` lease seconds so the route
    can answer ``409`` with enough for the caller to skip to the next ticket.
    """

    def __init__(self, owner: str | None, remaining: float) -> None:
        super().__init__(f"ticket held by {owner!r} ({remaining:.0f}s remaining)")
        self.owner = owner
        self.remaining = max(0.0, remaining)


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


def _meta_predicate(meta_filter: dict | None, *, col: str = "meta") -> tuple[str, list[object]]:
    """AND-clauses matching a JSONB ``col`` by scalar-equality OR array-membership.

    For each ``{key: value}`` a row qualifies when ``col->>key = value`` (scalar) OR
    — when ``col->key`` is a JSON array — ``value`` is a MEMBER of it. Identical
    semantics to ``facts_by`` (PR #113), factored out so the single-table search
    predicate (``_where``) and the live+mounted union (``overlay_search``) stay in
    lockstep. ``col`` lets a union branch qualify the column if ever needed; each
    overlay subquery is its own SELECT so the bare ``meta`` works there too. Returns
    ``("", [])`` when ``meta_filter`` is empty, so callers can append unconditionally.
    """
    sql = ""
    params: list[object] = []
    for key, value in (meta_filter or {}).items():
        sql += (
            f" AND ({col}->>%s = %s OR (jsonb_typeof({col}->%s) = 'array' "
            f"AND {col}->%s @> %s::jsonb))"
        )
        params.extend([key, value, key, key, json.dumps([value])])
    return sql, params


class PostgresVectorGraph(SearchableGraph):
    """A pgvector-backed fact store bound to one partition.

    Working memory: ``PostgresVectorGraph(conn, org_id, user_id)`` — keyed
    ``(org_id, user_id)``. Snapshot: ``PostgresVectorGraph(conn, org_id,
    facts_table='snapshots', space=..., snapshot=...)`` — keyed
    ``(org_id, space, snapshot)``, ``user_id`` unused.
    """

    def __init__(
        self,
        conn: psycopg.Connection,
        org_id: str,
        user_id: str = "default",
        *,
        embedder: Embedder | None = None,
        policy: list[WriteStep] | None = None,
        recall_floor: float = 0.45,
        recall_k: int = 5,
        semantic_recall_floor: float = 0.30,
        semantic_recall_k: int = 10,
        facts_table: str = "facts",
        space: str | None = None,
        snapshot: str | None = None,
    ) -> None:
        # Validate the facts table against the allowlist before it reaches SQL; the
        # edge/claim satellites are derived from it (never caller-controlled).
        if facts_table not in _ALLOWED_FACTS_TABLES:
            raise ValueError(
                f"facts_table must be one of {sorted(_ALLOWED_FACTS_TABLES)}, got {facts_table!r}"
            )
        # ``facts_table='snapshots'`` binds this graph to ONE org-shared snapshot via
        # ``(space, snapshot)`` — every read/write is scoped to that key and
        # ``user_id`` is unused. Both are required for the snapshots table (NOT NULL)
        # and must be None for working memory (``facts`` has no space/snapshot cols).
        is_snapshot = facts_table == "snapshots"
        if is_snapshot and (space is None or snapshot is None):
            raise ValueError("space and snapshot are required when facts_table='snapshots'")
        if not is_snapshot and (space is not None or snapshot is not None):
            raise ValueError("space/snapshot must be None for the live facts table")
        self._facts_table = facts_table
        self._edges_table, self._claims_table = _SATELLITE_TABLES[facts_table]
        self._is_snapshot = is_snapshot
        self.space = space
        self.snapshot = snapshot
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

    # --- partition keying (working memory vs snapshot) ---------------------
    # One graph = one partition. These helpers emit that partition's key so every
    # read/write predicate is exactly it — ``(org_id, user_id)`` for working memory
    # or ``(org_id, space, snapshot)`` for a snapshot. There is NO ``shared``
    # disjunction: org-sharing of snapshots is authorized one layer up by org
    # membership; a snapshot graph only ever sees its own ``(space, snapshot)``.
    def _key_cols(self) -> list[str]:
        return ["org_id", "space", "snapshot"] if self._is_snapshot else ["org_id", "user_id"]

    def _key_vals(self) -> list[object]:
        if self._is_snapshot:
            return [self.org_id, self.space, self.snapshot]
        return [self.org_id, self.user_id]

    def _key_pred(self, alias: str = "") -> tuple[str, list[object]]:
        """This graph's partition predicate (no leading ``AND``) + its params.

        ``alias`` qualifies the columns (e.g. ``e`` -> ``e.org_id``) for joins.
        """
        p = f"{alias}." if alias else ""
        cols = " AND ".join(f"{p}{c} = %s" for c in self._key_cols())
        return cols, list(self._key_vals())

    def _join_keys(self, a: str, b: str) -> str:
        """Equijoin fragment tying two aliases on the partition key columns."""
        return " AND ".join(f"{a}.{c} = {b}.{c}" for c in self._key_cols())

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
        Writes are stamped with this graph's partition key (working memory or one
        snapshot); ``meta`` persists into the ``meta`` jsonb column.

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
            space=self.space,
            snapshot=self.snapshot,
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
        # Recursive walk up the derivation chain (dst -> src), carrying the visited
        # path to break cycles, bounded by ``max_depth``. Both the anchor and the
        # recursive step are scoped to this graph's partition key.
        anchor_pred, anchor_params = self._key_pred()
        recur_pred, recur_params = self._key_pred("e")
        sql = (
            "WITH RECURSIVE deps(id, depth, path) AS ("
            f"  SELECT src_id, 1, ARRAY[src_id] FROM {self._edges_table} "
            f"   WHERE {anchor_pred} AND kind=%s AND dst_id=%s "
            "  UNION ALL "
            "  SELECT e.src_id, d.depth+1, d.path || e.src_id "
            f"   FROM {self._edges_table} e JOIN deps d ON e.dst_id = d.id "
            f"   WHERE {recur_pred} AND e.kind=%s "
            "     AND d.depth < %s AND NOT (e.src_id = ANY(d.path))"
            ") SELECT DISTINCT id FROM deps"
        )
        params = [*anchor_params, kind, fact_id, *recur_params, kind, max_depth]
        rows = self._conn.execute(sql, params).fetchall()
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
        key_pred, params = self._key_pred()
        params.append(STALE_DERIVED_EDGE)
        rows = self._conn.execute(
            f"SELECT DISTINCT src_id FROM {self._edges_table} "
            f"WHERE {key_pred} AND kind=%s",
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
        key_pred, key_params = self._key_pred()
        self._conn.execute(
            f"UPDATE {self._facts_table} SET state = 'rejected' "
            f"WHERE {key_pred} AND id = %s",
            (*key_params, loser_id),
        )
        self.add_edge(winner_id, loser_id, "supersedes")
        self._flag_stale_dependents(loser_id)  # H5: propagate to derived learnings

    def _persist_claims(self, fact_id: str, claims: list[Claim]) -> None:
        """Replace the stored claims for ``fact_id`` with ``claims``.

        Delete-then-insert so a rewritten fact's claims stay in sync. Subject and
        attribute are stored normalized (the slot index matches on them); value is
        raw. Rows are stamped with this graph's partition key.
        """
        key_pred, key_params = self._key_pred()
        self._conn.execute(
            f"DELETE FROM {self._claims_table} WHERE {key_pred} AND fact_id=%s",
            (*key_params, fact_id),
        )
        key_cols = self._key_cols()
        for seq, c in enumerate(claims):
            cols = [*key_cols, "fact_id", "seq", "subject", "attribute", "value", "functional"]
            vals: list[object] = [
                *self._key_vals(), fact_id, seq, Claim.norm(c.subject),
                Claim.norm(c.attribute), c.value, c.functional,
            ]
            placeholders = ", ".join(["%s"] * len(vals))
            self._conn.execute(
                f"INSERT INTO {self._claims_table} ({', '.join(cols)}) "
                f"VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                vals,
            )

    def claims_for(self, fact_id: str) -> list[Claim]:
        """Stored claims for one fact, in seq order."""
        key_pred, params = self._key_pred()
        params.append(fact_id)
        rows = self._conn.execute(
            f"SELECT subject, attribute, value, functional FROM {self._claims_table} "
            f"WHERE {key_pred} AND fact_id=%s ORDER BY seq",
            params,
        ).fetchall()
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
        categories: list[str] | None = None,
        meta_filter: dict | None = None,
        decay: bool = True,
    ) -> list[SearchHit]:
        """Retrieve relevant facts. Pure pgvector cosine by default; ``hybrid=True``
        additionally fuses a BM25 keyword branch.

        ``categories`` (positive category membership), ``scope``, and ``meta_filter``
        (JSONB scalar-OR-array, like ``facts_by``) narrow the SIMILARITY ranking to a
        subset — e.g. the ``check`` facts with ``meta.scope='planning'`` most similar
        to ``query`` — without changing the ranking itself. Applied to both branches
        via ``_where``; omitting them is identical to prior behavior.

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
                exclude_categories=exclude_categories, categories=categories,
                meta_filter=meta_filter, apply_decay=apply_decay,
            )
        sem = self._search_vec(
            qvec, top_k=_FUSION_BRANCH_N, filters=filters, scope=scope, state=state, as_of=as_of,
            exclude_categories=exclude_categories, categories=categories,
            meta_filter=meta_filter, apply_decay=apply_decay,
        )
        kw = self._search_keyword(
            query, top_k=_FUSION_BRANCH_N, filters=filters, scope=scope, state=state, as_of=as_of,
            exclude_categories=exclude_categories, categories=categories,
            meta_filter=meta_filter,
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
        categories: list[str] | None = None,
        meta_filter: dict | None = None,
    ) -> tuple[str, list[object]]:
        """The shared partition/state/scope/filter predicate for a search branch.

        Returns ``(sql_fragment, params)`` so the cosine and keyword branches apply
        the exact same row gating — only their ranking expression differs. The base
        is this graph's partition key: ``(org_id, user_id)`` for working memory or
        ``(org_id, space, snapshot)`` for a snapshot (which only ever sees its own
        rows, so recall/dedup/conflict stay within it).

        ``categories`` (positive) keeps only rows whose ``category`` is in the list
        (``= ANY``, served by the ``category`` btree) — the complement of
        ``exclude_categories``. ``meta_filter`` adds the same JSONB scalar-OR-array
        predicate as ``facts_by`` (served by the ``(meta)`` GIN index). Both are
        AND-combined with the rest; omitting them is unchanged.
        """
        key_pred, params = self._key_pred()
        sql = f"WHERE {key_pred}"
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
        if state is not None:
            sql += " AND state = %s"
            params.append(state)
        if scope is not None:
            sql += " AND scope = %s"
            params.append(scope)
        for key, value in (filters or {}).items():
            sql += f" AND {key} = %s"
            params.append(value)
        # Positive category membership (complement of exclude_categories below).
        if categories:
            sql += " AND category = ANY(%s)"
            params.append(list(categories))
        # H2 exclusion: omit listed categories (NULL category is never excluded).
        if exclude_categories:
            sql += " AND (category IS NULL OR category <> ALL(%s))"
            params.append(list(exclude_categories))
        meta_sql, meta_params = _meta_predicate(meta_filter)
        sql += meta_sql
        params.extend(meta_params)
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
        categories: list[str] | None = None,
        meta_filter: dict | None = None,
        apply_decay: bool = False,
    ) -> list[SearchHit]:
        where, where_params = self._where(
            filters=filters, scope=scope, state=state, as_of=as_of,
            exclude_categories=exclude_categories, categories=categories,
            meta_filter=meta_filter,
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
        # Outcome/trust weighting (H1) lives only on working memory: the `snapshots`
        # table omits the outcome-trust columns (success/failure/last_outcome), so a
        # snapshot-bound search skips the multiplier entirely (neutral 1.0).
        outcome_sql = (
            "" if self._is_snapshot else
            " * CASE WHEN success_count + failure_count = 0 THEN 1.0 "
            "ELSE (success_count + 0.5) / (success_count + failure_count + 1.0) END"
        )
        sql = (
            "SELECT id, text, source, confidence, scope, category, observation_count, state, "
            f"(1 - (embedding <=> %s)){outcome_sql}{decay_sql} AS score "
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
        # Outcome-trust columns exist only on working memory; a snapshot is an
        # immutable saved state with no such columns, so this is facts-only.
        if self._is_snapshot:
            raise ValueError("record_outcome is only valid on working memory (facts)")
        column = "success_count" if success else "failure_count"
        outcome = "succeeded" if success else "failed"
        key_pred, key_params = self._key_pred()
        self._conn.execute(
            f"UPDATE {self._facts_table} SET {column} = {column} + 1, last_outcome = %s "
            f"WHERE {key_pred} AND id = %s",
            (outcome, *key_params, fact_id),
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
        categories: list[str] | None = None,
        meta_filter: dict | None = None,
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
            exclude_categories=exclude_categories, categories=categories,
            meta_filter=meta_filter,
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
        categories: list[str] | None = None,
        scope: str | None = None,
        meta_filter: dict | None = None,
    ) -> list[SearchHit]:
        """Vector-search the live graph unioned with mounted snapshots, in one query.

        Backs the mounted read-only overlay (see ``overlay_graph.py``). ``mounts``
        is a list of ``(space, snapshot)`` pairs naming org-shared snapshots to also
        expose. The query is embedded **once** and a single ``UNION ALL`` ranks the
        live ``facts`` branch and the ``snapshots`` branch together — no per-mount
        round trip, no re-embedding. Both tables have an HNSW embedding index, so
        each branch is a sub-linear indexed search.

        The live branch keeps the working-memory predicate (``org_id AND user_id``);
        the mounted branch is org-scoped and keyed by snapshot
        (``org_id AND (space, snapshot) ∈ mounts``) — the same within-org trust
        boundary :class:`OrgSourceReader` relies on, with org membership validated by
        the mount route. Mounted hits carry ``fact.meta["mountedFrom"]`` (a
        ``{"space","snapshot"}`` dict) so callers can tell them from live facts.
        Results are deduped by id (a live fact wins over a same-id snapshot copy),
        ranked by score, and truncated to ``top_k``.
        """
        qvec = _fit(self._embed(query))
        cols = (
            "id, text, source, confidence, scope, category, observation_count, state"
        )
        # Apply the SAME row gating to BOTH the live and mounted branches so a
        # mounted snapshot can't leak rows the filter should drop: H2 category
        # exclusion, positive category membership, scope, and the JSONB meta filter
        # (same semantics as the single-table _where / facts_by). Built once as a
        # shared fragment + params, injected into each branch in declaration order.
        excl = " AND (category IS NULL OR category <> ALL(%s))" if exclude_categories else ""
        excl_param: list[object] = [list(exclude_categories)] if exclude_categories else []
        cat = " AND category = ANY(%s)" if categories else ""
        cat_param: list[object] = [list(categories)] if categories else []
        scope_sql = " AND scope = %s" if scope is not None else ""
        scope_param: list[object] = [scope] if scope is not None else []
        meta_sql, meta_param = _meta_predicate(meta_filter)
        filt = f"{excl}{cat}{scope_sql}{meta_sql}"
        filt_param: list[object] = [*excl_param, *cat_param, *scope_param, *meta_param]
        live = (
            f"SELECT {cols}, NULL::text AS mount_space, NULL::text AS mount_snapshot, "
            "1 - (embedding <=> %s) AS score FROM facts "
            "WHERE org_id = %s AND user_id = %s "
            f"AND state = 'active' AND embedding IS NOT NULL{filt} "
            "ORDER BY embedding <=> %s LIMIT %s"
        )
        params: list[object] = [qvec, self.org_id, self.user_id, *filt_param, qvec, top_k]
        sql = f"SELECT * FROM ({live}) AS live"
        if mounts:
            ors = " OR ".join(["(space = %s AND snapshot = %s)"] * len(mounts))
            mounted = (
                f"SELECT {cols}, space AS mount_space, snapshot AS mount_snapshot, "
                "1 - (embedding <=> %s) AS score FROM snapshots "
                f"WHERE org_id = %s AND ({ors}) "
                f"AND state = 'active' AND embedding IS NOT NULL{filt} "
                "ORDER BY embedding <=> %s LIMIT %s"
            )
            sql += f" UNION ALL SELECT * FROM ({mounted}) AS mounted"
            params += [qvec, self.org_id]
            for space, snapshot in mounts:
                params += [space, snapshot]
            params += [*filt_param, qvec, top_k]
        rows = self._conn.execute(sql, params).fetchall()

        # Each branch is capped at top_k, so this is at most ~2*top_k rows. Dedupe
        # by id preferring the live copy (mount_space IS NULL), then rank + cap.
        best: dict[str, SearchHit] = {}
        for r in rows:
            mount_space, mount_snapshot, score = r[8], r[9], float(r[10])
            hit = SearchHit(
                fact=Fact(
                    id=r[0], text=r[1], source=r[2],
                    confidence=r[3] if r[3] is not None else 1.0,
                    scope=r[4], category=r[5], observation_count=r[6], state=r[7],
                ),
                score=score,
            )
            if mount_space is not None:
                hit.fact.meta["mountedFrom"] = {
                    "space": mount_space,
                    "snapshot": mount_snapshot,
                }
            existing = best.get(hit.fact.id)
            if existing is None:
                best[hit.fact.id] = hit
                continue
            # Prefer a live hit over a same-id snapshot copy; else higher score.
            existing_mounted = bool(existing.fact.meta.get("mountedFrom"))
            this_mounted = mount_space is not None
            if existing_mounted and not this_mounted:
                best[hit.fact.id] = hit
            elif existing_mounted == this_mounted and hit.score > existing.score:
                best[hit.fact.id] = hit
        ranked = sorted(best.values(), key=lambda h: h.score, reverse=True)
        return ranked[:top_k]

    def recent_cache(self, *, space: str, snapshot: str, limit: int) -> list[Fact]:
        """Newest active facts of an org-shared snapshot — the no-query overlay read path."""
        rows = self._conn.execute(
            "SELECT id, text, source, confidence, scope, category, observation_count, state "
            "FROM snapshots WHERE org_id = %s AND space = %s AND snapshot = %s "
            "AND state = 'active' ORDER BY created_at DESC LIMIT %s",
            (self.org_id, space, snapshot, limit),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    # --- dashboard snapshot (the graph the dashboard renders) --------------
    def active_facts(self) -> list[Fact]:
        """Every ``active`` fact for this tenant — the graph ``search`` reads.

        This is the one-to-one source for the dashboard graph view: the same
        rows ``read``/``search`` retrieve from, so what the dashboard shows is
        exactly what MCP ``get_context`` can recall (newest-first for display).
        """
        key_pred, key_params = self._key_pred()
        rows = self._conn.execute(
            "SELECT id, text, source, confidence, scope, category, observation_count, "
            "state, created_at, meta, cluster_id, cluster_label "
            f"FROM {self._facts_table} WHERE {key_pred} "
            "AND state = 'active' ORDER BY created_at DESC",
            key_params,
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def active_edges(self) -> list[tuple[str, str, str]]:
        """``(src, dst, kind)`` edges between this partition's active facts."""
        key_pred, key_params = self._key_pred("e")
        rows = self._conn.execute(
            f"SELECT e.src_id, e.dst_id, e.kind FROM {self._edges_table} e "
            f"JOIN {self._facts_table} s ON {self._join_keys('s', 'e')} AND s.id = e.src_id "
            f"JOIN {self._facts_table} d ON {self._join_keys('d', 'e')} AND d.id = e.dst_id "
            f"WHERE {key_pred} AND s.state = 'active' AND d.state = 'active'",
            key_params,
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    # --- clustering (navigation-only topic super-nodes) --------------------
    def recluster(self, *, min_cluster_size: int | None = None) -> int:
        """Define-pass: (re)assign topic clusters over this graph's facts, persisted.

        Runs the embed -> reduce -> HDBSCAN -> label pipeline over every fact in
        this partition (the working-memory graph, or one snapshot) and writes the
        resulting ``cluster_id``/``cluster_label`` back to each row.
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
        key_pred, key_vals = self._key_pred()
        sql = (
            f"UPDATE {self._facts_table} SET cluster_id = %s, cluster_label = %s "
            f"WHERE {key_pred} AND id = %s"
        )
        for fact in facts:
            self._conn.execute(
                sql,
                [fact.cluster_id, fact.cluster_label, *key_vals, fact.id],
            )
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
        """Every fact in this partition (optionally filtered by ``state``), newest first."""
        key_pred, params = self._key_pred()
        sql = (
            "SELECT id, text, source, confidence, scope, category, observation_count, "
            "state, created_at, meta, cluster_id, cluster_label "
            f"FROM {self._facts_table} WHERE {key_pred}"
        )
        if state is not None:
            sql += " AND state = %s"
            params.append(state)
        sql += " ORDER BY created_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def facts_by(
        self,
        *,
        category: str | None = None,
        source: str | None = None,
        scope: str | None = None,
        state: str | None = "active",
        meta_filter: dict | None = None,
    ) -> list[Fact]:
        """EXHAUSTIVE, server-side filtered enumeration of facts — no top-k, no ranking.

        The completeness primitive the coverage spine needs: ``get_context`` is a
        semantic top-k that *samples* (it can silently drop a match) and ``all_facts``
        has no category/meta filter; this returns EVERY active fact matching the given
        column/meta predicates in one indexed SQL query (not load-all-then-filter in
        Python), newest first.

        Column equality filters (each optional, AND-combined): ``category``, ``source``,
        ``scope`` (the top-level scope COLUMN — distinct from ``meta.scope``).
        ``state`` defaults to ``"active"``; pass ``state=None`` to enumerate across all
        lifecycle states.

        ``meta_filter`` matches the JSONB ``meta`` column: for each ``{key: value}`` the
        fact qualifies when ``meta.key == value`` (scalar equality) OR — when
        ``meta.key`` is a JSON array — ``value`` is a MEMBER of it. The array case lets
        a check tagged ``meta.applies_to=["s-home","*"]`` match a query for ``"s-home"``
        or ``"*"``. All keys must match (AND). Respects the same partition key
        (working memory or one snapshot) as the rest of the read surface.
        """
        key_pred, params = self._key_pred()
        sql = (
            "SELECT id, text, source, confidence, scope, category, observation_count, "
            "state, created_at, meta, cluster_id, cluster_label "
            f"FROM {self._facts_table} WHERE {key_pred}"
        )
        if state is not None:
            sql += " AND state = %s"
            params.append(state)
        if category is not None:
            sql += " AND category = %s"
            params.append(category)
        if source is not None:
            sql += " AND source = %s"
            params.append(source)
        if scope is not None:
            sql += " AND scope = %s"
            params.append(scope)
        for key, value in (meta_filter or {}).items():
            # Scalar equality OR array-membership, in one indexed JSONB predicate:
            #   meta->>key = value                      (scalar: "planning")
            #   jsonb_typeof(meta->key)='array' AND meta->key @> [value]  (list: applies_to)
            sql += (
                " AND (meta->>%s = %s OR (jsonb_typeof(meta->%s) = 'array' "
                "AND meta->%s @> %s::jsonb))"
            )
            params.extend([key, value, key, key, json.dumps([value])])
        sql += " ORDER BY created_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def get_fact(self, fact_id: str) -> Fact | None:
        key_pred, key_params = self._key_pred()
        rows = self._conn.execute(
            "SELECT id, text, source, confidence, scope, category, observation_count, "
            "state, created_at, meta, cluster_id, cluster_label "
            f"FROM {self._facts_table} WHERE {key_pred} AND id = %s",
            (*key_params, fact_id),
        ).fetchall()
        if not rows:
            return None
        return self._row_to_fact(rows[0])

    def set_state(self, fact_id: str, state: str) -> None:
        key_pred, key_params = self._key_pred()
        self._conn.execute(
            f"UPDATE {self._facts_table} SET state = %s WHERE {key_pred} AND id = %s",
            (state, *key_params, fact_id),
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
            loser_pred, loser_params = self._key_pred("loser")
            self._conn.execute(
                f"UPDATE {self._facts_table} AS loser "
                "SET invalid_at = COALESCE("
                f"  (SELECT winner.valid_at FROM {self._facts_table} AS winner "
                f"    WHERE {self._join_keys('winner', 'loser')} AND winner.id = %s), "
                "  now()) "
                f"WHERE {loser_pred} AND loser.id = %s",
                (winner_id, *loser_params, fact_id),
            )
        else:
            key_pred, key_params = self._key_pred()
            self._conn.execute(
                f"UPDATE {self._facts_table} SET invalid_at = now() "
                f"WHERE {key_pred} AND id = %s",
                (*key_params, fact_id),
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
        key_pred, key_params = self._key_pred()
        params.extend([*key_params, fact_id])
        self._conn.execute(
            f"UPDATE {self._facts_table} SET {', '.join(sets)} "
            f"WHERE {key_pred} AND id = %s",
            params,
        )

    def set_meta(self, fact_id: str, meta: dict) -> None:
        key_pred, key_params = self._key_pred()
        self._conn.execute(
            f"UPDATE {self._facts_table} SET meta = %s WHERE {key_pred} AND id = %s",
            (json.dumps(meta), *key_params, fact_id),
        )

    def delete_fact(self, fact_id: str) -> None:
        key_pred, key_params = self._key_pred()
        self._conn.execute(
            f"DELETE FROM {self._facts_table} WHERE {key_pred} AND id = %s",
            (*key_params, fact_id),
        )

    # --- edges (full lifecycle, candidate-facade surface) ------------------
    def add_edge(self, src_id: str, dst_id: str, kind: str = "contradiction") -> None:
        cols = [*self._key_cols(), "src_id", "dst_id", "kind"]
        vals: list[object] = [*self._key_vals(), src_id, dst_id, kind]
        placeholders = ", ".join(["%s"] * len(vals))
        self._conn.execute(
            f"INSERT INTO {self._edges_table} ({', '.join(cols)}) "
            f"VALUES ({placeholders}) ON CONFLICT DO NOTHING",
            vals,
        )

    def remove_edge(self, src_id: str, dst_id: str, kind: str = "contradiction") -> None:
        # Delete both directions: contradictions are conceptually undirected.
        key_pred, key_params = self._key_pred()
        self._conn.execute(
            f"DELETE FROM {self._edges_table} WHERE {key_pred} AND kind = %s "
            "AND ((src_id = %s AND dst_id = %s) OR (src_id = %s AND dst_id = %s))",
            [*key_params, kind, src_id, dst_id, dst_id, src_id],
        )

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
        """All (src, dst, kind) edges in this partition, regardless of fact state."""
        key_pred, params = self._key_pred()
        sql = f"SELECT src_id, dst_id, kind FROM {self._edges_table} WHERE {key_pred}"
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
        that re-reads one candidate) does not pay a full-partition edge scan."""
        key_pred, key_params = self._key_pred()
        rows = self._conn.execute(
            f"SELECT src_id, dst_id, kind FROM {self._edges_table} "
            f"WHERE {key_pred} AND (src_id = %s OR dst_id = %s)",
            [*key_params, fact_id, fact_id],
        ).fetchall()
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
        Scoped to this partition like ``all_facts``; newest first if (defensively) more than one.
        """
        key_pred, params = self._key_pred()
        sql = (
            "SELECT id, text, source, confidence, scope, category, observation_count, "
            "state, created_at, meta, cluster_id, cluster_label "
            f"FROM {self._facts_table} WHERE {key_pred} "
            "AND category = %s AND scope = %s AND meta->>'screen_id' = %s"
        )
        params.extend([SURFACE_CATEGORY, project, screen_id])
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
        key_pred, key_params = self._key_pred("e")
        rows = self._conn.execute(
            f"SELECT e.src_id, e.dst_id FROM {self._edges_table} e "
            f"JOIN {self._facts_table} s ON {self._join_keys('s', 'e')} AND s.id = e.src_id "
            f"JOIN {self._facts_table} d ON {self._join_keys('d', 'e')} AND d.id = e.dst_id "
            f"WHERE {key_pred} "
            "AND e.kind = %s AND s.state = 'active' AND d.state = 'active'",
            (*key_params, RENDERS_EDGE),
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

    def _facts_for_surface(
        self,
        project: str,
        screen_id: str,
        *,
        category: str | None = None,
        meta_scope: str | None = None,
    ) -> list[Fact]:
        """Active facts bound (RENDERS) to ``(project, screen_id)``, newest first.

        The shared join behind ``requirements_for_surface`` (no category filter -> the
        governing requirements) and ``checks_for_surface`` (category="check", optional
        ``meta_scope``). ``category``/``meta_scope`` are extra equality predicates on
        the source fact; with both ``None`` the result is exactly the original
        ``requirements_for_surface`` behavior. Empty when the surface is unknown.
        """
        surface = self._find_surface(project, screen_id)
        if surface is None:
            return []
        key_pred, params = self._key_pred("e")
        sql = (
            "SELECT r.id, r.text, r.source, r.confidence, r.scope, r.category, "
            "r.observation_count, r.state, r.created_at, r.meta, r.cluster_id, r.cluster_label "
            f"FROM {self._edges_table} e "
            f"JOIN {self._facts_table} r ON {self._join_keys('r', 'e')} AND r.id = e.src_id "
            f"WHERE {key_pred} "
            "AND e.kind = %s AND e.dst_id = %s AND r.state = 'active'"
        )
        params.extend([RENDERS_EDGE, surface.id])
        if category is not None:
            sql += " AND r.category = %s"
            params.append(category)
        if meta_scope is not None:
            sql += " AND r.meta->>'scope' = %s"
            params.append(meta_scope)
        sql += " ORDER BY r.created_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def requirements_for_surface(self, project: str, screen_id: str) -> list[Fact]:
        """PRIMARY query: active requirement facts that RENDER ``(project, screen_id)``.

        Joins ``fact_edges`` (kind=RENDERS_EDGE, dst=surface) to the source facts and
        returns the ``active`` ones, newest first. Empty when the surface is unknown.
        """
        return self._facts_for_surface(project, screen_id)

    def checks_for_surface(
        self, project: str, screen_id: str, scope: str | None = None
    ) -> list[Fact]:
        """All active ``check`` facts bound (RENDERS) to ``(project, screen_id)``.

        The surface-scoped convenience over :meth:`facts_by` for the coverage spine:
        the generalization of :meth:`requirements_for_surface` to ``category="check"``,
        reusing the same ``renders``-edge binding. ``scope`` (optional) narrows to a
        single coverage gate by matching ``meta.scope`` ("planning" | "validation").
        EXHAUSTIVE (every bound check, no top-k) and active-only, so a rejected check
        or surface drops out with no stale hook. Empty when the surface is unknown.
        """
        return self._facts_for_surface(
            project, screen_id, category=CHECK_CATEGORY, meta_scope=scope
        )

    def surfaces_for_requirement(self, requirement_fact_id: str) -> list[Fact]:
        """Active surface facts governed by ``requirement_fact_id`` (newest first).

        The dst side of the RENDERS edges out of the requirement, restricted to
        ``active`` surface facts.
        """
        key_pred, params = self._key_pred("e")
        sql = (
            "SELECT d.id, d.text, d.source, d.confidence, d.scope, d.category, "
            "d.observation_count, d.state, d.created_at, d.meta, d.cluster_id, d.cluster_label "
            f"FROM {self._edges_table} e "
            f"JOIN {self._facts_table} d ON {self._join_keys('d', 'e')} AND d.id = e.dst_id "
            f"WHERE {key_pred} "
            "AND e.kind = %s AND e.src_id = %s AND d.state = 'active' AND d.category = %s"
        )
        params.extend([RENDERS_EDGE, requirement_fact_id, SURFACE_CATEGORY])
        sql += " ORDER BY d.created_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def list_surface_bindings(self, project: str) -> list[dict]:
        """Every RENDERS edge whose dst surface fact has ``scope = project`` (any state).

        Returns ``{"requirementId","surfaceId","screenId"}`` per edge; ``screenId``
        comes from the surface fact's ``meta->>'screen_id'``.
        """
        key_pred, params = self._key_pred("e")
        sql = (
            "SELECT e.src_id, e.dst_id, d.meta->>'screen_id' "
            f"FROM {self._edges_table} e "
            f"JOIN {self._facts_table} d ON {self._join_keys('d', 'e')} AND d.id = e.dst_id "
            f"WHERE {key_pred} "
            "AND e.kind = %s AND d.category = %s AND d.scope = %s"
        )
        params.extend([RENDERS_EDGE, SURFACE_CATEGORY, project])
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
        success_count: int,
        last_outcome: str | None,
        is_stale: bool,
        build_state: str | None = None,
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

        ``build_state`` (the ticket lifecycle enum carried in ``meta``) is AUTHORITATIVE
        and overrides the count-derived classification when set to a known value:
          * ``finished``    -> COMPLETE (``[]``), regardless of counts/last_outcome;
          * ``incomplete``  -> INCOMPLETE, reason ``reopened`` (a deliberately re-opened
            ticket), regardless of a prior success;
          * ``in_progress`` -> INCOMPLETE, reason ``in_progress`` (actively being built),
            so it stays in the incomplete set while claimed.
        Any other value (absent / null / unknown enum) FALLS BACK to the count-derived
        never-built / regressed / stale logic so existing tickets carrying no enum are
        not mass-reclassified.
        """
        if build_state == "finished":
            return []
        if build_state == "incomplete":
            return ["reopened"]
        if build_state == "in_progress":
            return ["in_progress"]
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
        key_pred, params = self._key_pred()
        params.extend([REQUIREMENT_CATEGORY, source])
        # Outcome-trust columns live only on working memory; a snapshot omits them,
        # so project the neutral defaults there (never-built until loaded live).
        outcome_cols = (
            "0, 0, NULL::text" if self._is_snapshot
            else "success_count, failure_count, last_outcome"
        )
        rows = self._conn.execute(
            "SELECT id, text, source, confidence, scope, category, observation_count, "
            "state, created_at, meta, cluster_id, cluster_label, "
            f"{outcome_cols} "
            f"FROM {self._facts_table} "
            f"WHERE {key_pred} AND state = 'active' "
            "AND category = %s AND source = %s "
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
            build_state = (fact.meta or {}).get("build_state")
            reasons = self._completeness_reasons(
                success_count,
                last_outcome,
                fact.id in stale_ids,
                build_state=build_state,
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

    def incomplete_requirements(
        self, project: str, exclude_leased: bool = False
    ) -> list[dict]:
        """Active requirement facts in ``prd-<project>`` that are NOT verified-complete.

        The primary query the agent-factory loop calls to pick "the next unbuilt
        requirement". Completeness is DERIVED from verification + staleness signals
        only (``record_outcome`` results + ``_flag_stale_dependents`` flags) — there is
        no agent-settable completeness column. A requirement is incomplete when it has
        never succeeded (never-built), most-recently failed after a prior success
        (regressed — the bug/ticket path), or is flagged stale (a dependency changed).

        Returns one entry per incomplete requirement, newest first::

            {"fact": Fact, "reason": str, "reasons": list[str],
             "success_count": int, "failure_count": int, "last_outcome": str | None,
             "claim": {"build_state", "claim_owner", "claim_heartbeat_at", "lease_live"}}

        ``reason`` is the primary cause; ``reasons`` lists every cause that applies.
        Complete requirements (latest outcome succeeded, not stale) are omitted; so are
        rejected/superseded ones (active-only). See ``_completeness_reasons``.

        ``claim`` exposes the build-loop lease so a selector can skip a ticket another
        agent already holds (see ``claim_requirement``). With ``exclude_leased`` true,
        tickets with a LIVE lease are omitted entirely (stale-leased and unclaimed
        ones remain — they are claimable); default false preserves prior behavior.
        """
        now = self._server_epoch()
        out: list[dict] = []
        for item in self._classify_project_requirements(project):
            if not item["reasons"]:
                continue
            claim = self._claim_view(item["fact"].meta or {}, now)
            if exclude_leased and claim["lease_live"]:
                continue
            out.append({**item, "claim": claim})
        return out

    def completeness_summary(self, project: str) -> dict:
        """Done-of-definition counts for ``prd-<project>``'s active requirements.

        ``{total_active_requirements, complete, incomplete, breakdown}`` where
        ``breakdown`` counts incomplete requirements by primary reason
        (``never_built`` / ``stale`` / ``regressed``, plus ``reopened`` / ``in_progress``
        when a ticket's authoritative ``build_state`` overrides the count derivation)
        and sums to ``incomplete`` (the primary reason partitions the set — see
        ``_completeness_reasons``). Derived from the same verification + staleness
        signals (and ``build_state`` override) as ``incomplete_requirements``. The
        ``build_state``-driven keys appear only when present, so the breakdown stays a
        three-key dict for plans that carry no enum.
        """
        classified = self._classify_project_requirements(project)
        incomplete = [item for item in classified if item["reasons"]]
        breakdown = {"never_built": 0, "stale": 0, "regressed": 0}
        for item in incomplete:
            key = item["reason"].replace("-", "_")
            breakdown[key] = breakdown.get(key, 0) + 1
        total = len(classified)
        return {
            "total_active_requirements": total,
            "complete": total - len(incomplete),
            "incomplete": len(incomplete),
            "breakdown": breakdown,
        }

    # --- ticket lease/claim (multi-agent build loop) -----------------------
    def _server_epoch(self) -> float:
        """The DB's wall clock as epoch seconds — the single trusted lease clock.

        Lease liveness is decided server-side (never from a client timestamp), so
        every claim/heartbeat/expiry comparison reads ``now`` from the same source
        the SQL grant conditions use (``EXTRACT(EPOCH FROM now())``).
        """
        row = self._conn.execute("SELECT EXTRACT(EPOCH FROM now())").fetchone()
        return float(row[0])

    @staticmethod
    def _claim_view(meta: dict, now: float) -> dict:
        """The lease-facing projection of a fact's ``meta`` for the selector.

        ``lease_live`` is true iff the ticket is ``in_progress`` with a non-stale
        heartbeat (``now - claim_heartbeat_at <= claim_lease_ttl``). A stale or
        absent lease reads as not-live (the ticket is claimable).
        """
        meta = meta or {}
        build_state = meta.get("build_state")
        owner = meta.get("claim_owner")
        hb = meta.get("claim_heartbeat_at")
        ttl = meta.get("claim_lease_ttl")
        lease_live = (
            build_state == "in_progress"
            and owner is not None
            and hb is not None
            and ttl is not None
            and (now - float(hb)) <= float(ttl)
        )
        return {
            "build_state": build_state,
            "claim_owner": owner,
            "claim_heartbeat_at": hb,
            "lease_live": lease_live,
        }

    def _lease_conflict(self, fact_id: str) -> None:
        """Raise ``KeyError`` if the fact is gone, else ``LeaseConflict`` (held).

        Best-effort report for the 409 path: re-reads the (current) owner and the
        remaining lease seconds. Not part of the atomic grant — it only explains a
        grant/heartbeat that the single conditional UPDATE already declined.
        """
        key_pred, key_params = self._key_pred()
        row = self._conn.execute(
            "SELECT meta->>'claim_owner', "
            "COALESCE((meta->>'claim_lease_ttl')::float, 0) "
            "  - (EXTRACT(EPOCH FROM now()) "
            "     - COALESCE((meta->>'claim_heartbeat_at')::float, 0)) "
            f"FROM {self._facts_table} "
            f"WHERE {key_pred} AND id = %s",
            (*key_params, fact_id),
        ).fetchone()
        if row is None:
            raise KeyError(fact_id)
        raise LeaseConflict(owner=row[0], remaining=float(row[1] or 0.0))

    def claim_requirement(
        self, fact_id: str, owner: str, lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS
    ) -> dict:
        """Atomically lease a requirement ticket to ``owner`` (FOR the build loop).

        ATOMIC at the row level: a single conditional ``UPDATE ... WHERE`` grants the
        claim iff the ticket is NOT held by a different LIVE lease — i.e. it is not
        ``in_progress``, OR ``owner`` already holds it (idempotent renew), OR the
        existing lease is STALE (heartbeat older than its TTL → auto-reclaim a dead
        agent). Two concurrent claims for the same ticket yield exactly one grant:
        the loser re-evaluates the WHERE against the committed row and matches no
        row. On grant returns the claim view; on conflict raises :class:`LeaseConflict`
        (held by a live owner) and on an unknown fact ``KeyError``.
        """
        key_pred, key_params = self._key_pred()
        row = self._conn.execute(
            f"UPDATE {self._facts_table} SET meta = meta || jsonb_build_object("
            "  'build_state', 'in_progress', "
            "  'claim_owner', %s::text, "
            "  'claim_at', EXTRACT(EPOCH FROM now()), "
            "  'claim_heartbeat_at', EXTRACT(EPOCH FROM now()), "
            "  'claim_lease_ttl', %s::int) "
            f"WHERE {key_pred} AND id = %s AND ("
            "  COALESCE(meta->>'build_state', '') <> 'in_progress' "
            "  OR meta->>'claim_owner' = %s "
            "  OR EXTRACT(EPOCH FROM now()) - COALESCE((meta->>'claim_heartbeat_at')::float, 0) "
            "     > COALESCE((meta->>'claim_lease_ttl')::float, 0)) "
            "RETURNING meta",
            (
                owner,
                int(lease_ttl_seconds),
                *key_params,
                fact_id,
                owner,
            ),
        ).fetchone()
        if row is None:
            self._lease_conflict(fact_id)  # raises KeyError or LeaseConflict
        meta = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return self._claim_view(meta, self._server_epoch())

    def heartbeat_requirement(self, fact_id: str, owner: str) -> dict:
        """Renew ``owner``'s live lease on a ticket (bump the heartbeat).

        Bumps ``claim_heartbeat_at`` iff ``owner`` still holds a LIVE lease. If the
        lease was lost (owner changed) or expired, no row matches → the caller is
        told to stop working it (:class:`LeaseConflict`); an unknown fact is
        ``KeyError``.
        """
        key_pred, key_params = self._key_pred()
        row = self._conn.execute(
            f"UPDATE {self._facts_table} SET meta = meta || jsonb_build_object("
            "  'claim_heartbeat_at', EXTRACT(EPOCH FROM now())) "
            f"WHERE {key_pred} AND id = %s "
            "  AND meta->>'claim_owner' = %s "
            "  AND meta->>'build_state' = 'in_progress' "
            "  AND EXTRACT(EPOCH FROM now()) - COALESCE((meta->>'claim_heartbeat_at')::float, 0) "
            "      <= COALESCE((meta->>'claim_lease_ttl')::float, 0) "
            "RETURNING meta",
            (*key_params, fact_id, owner),
        ).fetchone()
        if row is None:
            self._lease_conflict(fact_id)
        meta = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return self._claim_view(meta, self._server_epoch())

    def release_requirement(self, fact_id: str, owner: str, state: str) -> dict:
        """Clear ``owner``'s lease and record a terminal ``build_state``.

        Sets ``build_state`` to ``finished`` (ticket done) or ``incomplete`` (yielded
        cleanly) and DROPS the lease keys (``claim_owner``/``claim_at``/
        ``claim_heartbeat_at``/``claim_lease_ttl``), MERGING into ``meta`` so every
        other key (``tags``/``surfaces``/``requirement_id``/``pinned_checks``...) is
        preserved. Only the holding owner may release; a mismatch is
        :class:`LeaseConflict` and an unknown fact ``KeyError``. ``finished`` clears
        the lease only — completeness stays derived from outcomes elsewhere.
        """
        if state not in ("finished", "incomplete"):
            raise ValueError("state must be 'finished' or 'incomplete'")
        # ``meta - 'k1' - 'k2' ...`` removes the lease keys; then merge the new
        # build_state. Both preserve every unrelated key.
        strip = " ".join(f"- '{k}'" for k in _LEASE_META_KEYS)
        key_pred, key_params = self._key_pred()
        row = self._conn.execute(
            f"UPDATE {self._facts_table} SET meta = (meta {strip}) "
            "  || jsonb_build_object('build_state', %s::text) "
            f"WHERE {key_pred} AND id = %s "
            "  AND meta->>'claim_owner' = %s "
            "RETURNING meta",
            (state, *key_params, fact_id, owner),
        ).fetchone()
        if row is None:
            self._lease_conflict(fact_id)
        meta = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return self._claim_view(meta, self._server_epoch())

    def wipe_cache(self) -> int:
        """Delete every fact in this snapshot-bound graph's ``(space, snapshot)``.

        Snapshot graphs only (built with ``facts_table='snapshots'``). Edges/claims
        cascade via the FK. Returns the number of facts removed.
        """
        if not self._is_snapshot:
            raise ValueError("wipe_cache requires a snapshot-bound graph")
        cur = self._conn.execute(
            "DELETE FROM snapshots WHERE org_id = %s AND space = %s AND snapshot = %s",
            (self.org_id, self.space, self.snapshot),
        )
        return cur.rowcount

    # --- snapshot save/load (org-shared snapshots + eval datasets) ----------
    def _require_live(self, op: str) -> None:
        if self._is_snapshot:
            raise ValueError(f"{op} must be called on the live working-memory (facts) graph")

    @staticmethod
    def _pairs_pred(pairs: list[tuple[str, str]]) -> tuple[str, list[object]]:
        """``(space, snapshot)`` membership predicate over a snapshot table + params."""
        ors = " OR ".join(["(space=%s AND snapshot=%s)"] * len(pairs))
        params: list[object] = []
        for space, snapshot in pairs:
            params.extend([space, snapshot])
        return f"({ors})", params

    # --- reload reconciliation (id stability + live-meta durability) --------
    @staticmethod
    def _load_meta(raw: Any) -> dict:
        """Coerce a ``meta`` column value (jsonb dict or json text) to a dict."""
        if isinstance(raw, dict):
            return raw
        if raw:
            try:
                return json.loads(raw)
            except (TypeError, ValueError):
                return {}
        return {}

    @staticmethod
    def _natural_key(meta: Any) -> str | None:
        """A fact's stable cross-materialization identity, or ``None``.

        A requirement carries ``meta.requirement_id`` (e.g. ``"R8"``) — the canonical
        key dependency edges already resolve by, stable even when two independent
        materializations of the same PRD assign different fact ids. Fall back to the
        first ``req:<name>`` ticket tag so a fact tagged but lacking ``requirement_id``
        still reconciles. Everything else has no natural key (reconciled by fact id
        only), so this returns ``None`` and the reload leaves it keyed on its own id.
        """
        if not isinstance(meta, dict):
            return None
        rid = meta.get("requirement_id")
        if rid not in (None, ""):
            return f"requirement_id:{rid}"
        for tag in meta.get("tags") or []:
            if isinstance(tag, str) and tag.startswith("req:"):
                return f"tag:{tag}"
        return None

    def _live_reload_state(self) -> tuple[dict[str, str], dict[str, dict]]:
        """Pre-reload snapshot of this working graph, taken inside the reload txn.

        Returns ``(id_by_key, agent_meta_by_id)``:
        - ``id_by_key`` maps each live fact's natural key -> its live fact id, so the
          reload can keep an existing requirement's id stable instead of adopting the
          snapshot's id for the same logical requirement.
        - ``agent_meta_by_id`` maps live fact id -> its agent-owned meta subset
          (:data:`_LIVE_AGENT_META_KEYS`), so the reload can merge that live state
          back OVER the snapshot baseline rather than reverting it.
        """
        rows = self._conn.execute(
            "SELECT id, meta FROM facts WHERE org_id=%s AND user_id=%s",
            (self.org_id, self.user_id),
        ).fetchall()
        id_by_key: dict[str, str] = {}
        agent_meta_by_id: dict[str, dict] = {}
        for fid, raw in rows:
            meta = self._load_meta(raw)
            key = self._natural_key(meta)
            if key is not None:
                id_by_key[key] = fid
            kept = {k: meta[k] for k in _LIVE_AGENT_META_KEYS if k in meta}
            if kept:
                agent_meta_by_id[fid] = kept
        return id_by_key, agent_meta_by_id

    def _snapshot_id_remap(
        self, pred: str, pred_params: list, id_by_key: dict[str, str]
    ) -> dict[str, str]:
        """Map snapshot fact id -> existing live id where they share a natural key.

        So a requirement the working graph already holds under id ``L`` keeps ``L``
        when the snapshot stores the same requirement under a different id ``S`` — the
        logical requirement never changes id across a reload. Ids that already match,
        or have no live counterpart, are absent (no remap needed).
        """
        if not id_by_key:
            return {}
        rows = self._conn.execute(
            f"SELECT id, meta FROM snapshots WHERE org_id=%s AND {pred}",
            (self.org_id, *pred_params),
        ).fetchall()
        remap: dict[str, str] = {}
        for sid, raw in rows:
            key = self._natural_key(self._load_meta(raw))
            if key is None:
                continue
            live_id = id_by_key.get(key)
            if live_id is not None and live_id != sid:
                remap[sid] = live_id
        return remap

    @staticmethod
    def _remap_values(remap: dict[str, str]) -> tuple[str, list]:
        """``(VALUES (%s::text,%s::text), ...)`` fragment + params for an id remap."""
        rows = ", ".join(["(%s::text, %s::text)"] * len(remap))
        params: list[object] = []
        for old, new in remap.items():
            params.extend([old, new])
        return f"(VALUES {rows})", params

    def _insert_snapshot_rows(
        self, pred: str, pred_params: list, remap: dict[str, str]
    ) -> None:
        """Copy the snapshot facts/edges/claims matching ``pred`` into working memory.

        With no ``remap`` this is the original bulk restamp copy. When ``remap`` maps
        snapshot ids -> existing live ids, the fact id, both edge endpoints, and the
        claim ``fact_id`` are rewritten through it (``COALESCE(remap.new_id, <col>)``)
        so a reconciled requirement lands under its live id and its edges/claims stay
        consistent (no dangling FK).
        """
        dst = (self.org_id, self.user_id)
        if not remap:
            self._conn.execute(
                f"INSERT INTO facts ({_FACT_COPY_COLS}) "
                f"SELECT {_FACT_RESTAMP_SELECT} FROM snapshots WHERE org_id=%s AND {pred}",
                (*dst, self.org_id, *pred_params),
            )
            self._conn.execute(
                "INSERT INTO fact_edges (org_id, user_id, src_id, dst_id, kind) "
                "SELECT %s, %s, src_id, dst_id, kind FROM snapshot_edges "
                f"WHERE org_id=%s AND {pred}",
                (*dst, self.org_id, *pred_params),
            )
            self._conn.execute(
                f"INSERT INTO claims ({_CLAIM_COPY_COLS}) "
                f"SELECT {_CLAIM_RESTAMP_SELECT} FROM snapshot_claims WHERE org_id=%s AND {pred}",
                (*dst, self.org_id, *pred_params),
            )
            return
        vals, vparams = self._remap_values(remap)
        # facts: leading ``id`` -> COALESCE(r.new_id, id); rest of the restamp select
        # (``, %s(org), %s(user), text, ...``) is unchanged.
        fact_select = "COALESCE(r.new_id, id)" + _FACT_RESTAMP_SELECT[len("id"):]
        self._conn.execute(
            f"INSERT INTO facts ({_FACT_COPY_COLS}) "
            f"SELECT {fact_select} FROM snapshots "
            f"LEFT JOIN {vals} AS r(old_id, new_id) ON r.old_id = id "
            f"WHERE org_id=%s AND {pred}",
            (*dst, *vparams, self.org_id, *pred_params),
        )
        self._conn.execute(
            "INSERT INTO fact_edges (org_id, user_id, src_id, dst_id, kind) "
            "SELECT %s, %s, COALESCE(rs.new_id, src_id), COALESCE(rd.new_id, dst_id), kind "
            "FROM snapshot_edges "
            f"LEFT JOIN {vals} AS rs(old_id, new_id) ON rs.old_id = src_id "
            f"LEFT JOIN {vals} AS rd(old_id, new_id) ON rd.old_id = dst_id "
            f"WHERE org_id=%s AND {pred}",
            (*dst, *vparams, *vparams, self.org_id, *pred_params),
        )
        claim_select = _CLAIM_RESTAMP_SELECT.replace(
            "fact_id", "COALESCE(r.new_id, fact_id)", 1
        )
        self._conn.execute(
            f"INSERT INTO claims ({_CLAIM_COPY_COLS}) "
            f"SELECT {claim_select} FROM snapshot_claims "
            f"LEFT JOIN {vals} AS r(old_id, new_id) ON r.old_id = fact_id "
            f"WHERE org_id=%s AND {pred}",
            (*dst, *vparams, self.org_id, *pred_params),
        )

    def _reapply_agent_meta(self, agent_meta_by_id: dict[str, dict]) -> None:
        """Merge each preserved live agent-meta subset back OVER the reloaded baseline.

        Right operand of ``||`` wins, so the live ``build_state``/``claim_*``/``run_*``/
        ``pinned_checks``/``required_validations``/``block_reason`` survive the reload.
        Keyed by the FINAL (live) fact id, so it lands on the id-reconciled row; a key
        whose fact was not reinserted (dropped by a ``replace`` that excluded it) is a
        harmless no-op.
        """
        for fid, kept in agent_meta_by_id.items():
            self._conn.execute(
                "UPDATE facts SET meta = COALESCE(meta, '{}'::jsonb) || %s::jsonb "
                "WHERE org_id=%s AND user_id=%s AND id=%s",
                (json.dumps(kept), self.org_id, self.user_id, fid),
            )

    def save_cache(self, space: str, snapshot: str) -> int:
        """Dump this user's working memory into the org-shared snapshot ``(space, snapshot)``.

        Pure SQL copy — embeddings, ids, states and meta are preserved verbatim, so a
        later ``load_cache`` restores losslessly with no re-embedding. Any existing
        rows under ``(space, snapshot)`` are replaced. The snapshot drops ``user_id``
        (it is org-shared, not per-user). Returns facts copied.
        """
        self._require_live("save_cache")
        key = (self.org_id, space, snapshot)
        src = (self.org_id, self.user_id)
        # WRITE-TIME SECTION INVARIANT (the primary leak site — this dumps the WHOLE working graph).
        # A save into a kinded snapshot (prd-* plan / *-validation) must not carry a forbidden fact;
        # probe working memory for violators FIRST and refuse the whole dump if any exist, rather than
        # silently embedding them (the "11 checks in a prd snapshot" failure).
        violator = _snapshot_violator_sql(_snapshot_kind(snapshot))
        if violator is not None:
            row = self._conn.execute(
                f"SELECT count(*) FROM facts WHERE org_id=%s AND user_id=%s AND ({violator})",
                src,
            ).fetchone()
            n = int(row[0]) if row else 0
            if n:
                raise SnapshotKindError(
                    f"{n} fact(s) in working memory violate the "
                    f"{_snapshot_kind(snapshot)!r} kind of snapshot {snapshot!r}; e.g. "
                    f"category={CHECK_CATEGORY!r} facts cannot be saved into a prd-* plan — save "
                    f"them to building-validation/planning-validation instead"
                )
        # Replace any prior state under this key (edges/claims first for the FK).
        self._conn.execute(
            "DELETE FROM snapshot_edges WHERE org_id=%s AND space=%s AND snapshot=%s", key
        )
        self._conn.execute(
            "DELETE FROM snapshot_claims WHERE org_id=%s AND space=%s AND snapshot=%s", key
        )
        self._conn.execute(
            "DELETE FROM snapshots WHERE org_id=%s AND space=%s AND snapshot=%s", key
        )
        self._conn.execute(
            f"INSERT INTO snapshots ({_SNAPSHOT_COPY_COLS}, space, snapshot) "
            f"SELECT {_SNAPSHOT_COPY_COLS}, %s, %s FROM facts WHERE org_id=%s AND user_id=%s",
            (space, snapshot, *src),
        )
        self._conn.execute(
            "INSERT INTO snapshot_edges (org_id, space, snapshot, src_id, dst_id, kind) "
            "SELECT org_id, %s, %s, src_id, dst_id, kind FROM fact_edges "
            "WHERE org_id=%s AND user_id=%s",
            (space, snapshot, *src),
        )
        self._conn.execute(
            f"INSERT INTO snapshot_claims ({_SNAPSHOT_CLAIM_COPY_COLS}, space, snapshot) "
            f"SELECT {_SNAPSHOT_CLAIM_COPY_COLS}, %s, %s FROM claims WHERE org_id=%s AND user_id=%s",
            (space, snapshot, *src),
        )
        row = self._conn.execute(
            "SELECT count(*) FROM snapshots WHERE org_id=%s AND space=%s AND snapshot=%s", key
        ).fetchone()
        return int(row[0]) if row else 0

    def load_cache(self, space: str, snapshot: str) -> int:
        """Replace working memory with one org-shared snapshot (see ``load_caches``)."""
        return self.load_caches([(space, snapshot)])

    def load_caches(self, keys: list[tuple[str, str]]) -> int:
        """Replace this user's working memory with the union of the given snapshots.

        Full truncate + insert for this user's ``facts``/``fact_edges``/``claims``:
        those rows are deleted and replaced by the rows of every ``(space, snapshot)``
        in ``keys`` (embeddings and all), stamped with the loader's ``user_id`` (the
        snapshot carries none). A snapshot load passes one pair; an eval folder load
        passes its cases' ``('__evals__', <case_id>)`` pairs. Returns facts loaded.

        The whole swap runs in ONE transaction, so a concurrent reader never observes
        the truncated-but-not-yet-refilled graph (the connection is autocommit, so
        without this each DELETE/INSERT would commit on its own and reads landing
        between them would see a partial — non-deterministically sized — set). The
        reload is a MERGE-PRESERVING upsert, not a blind overwrite: a requirement the
        working graph already holds keeps its live fact id (reconciled by natural key
        — ``meta.requirement_id`` / ``req:`` tag) instead of adopting the snapshot's
        id, and its live agent-owned meta (build lifecycle / lease / verification
        contract) is merged back over the snapshot baseline.
        """
        self._require_live("load_caches")
        pairs = list(keys)
        dst = (self.org_id, self.user_id)
        with self._conn.transaction():
            id_by_key, agent_meta_by_id = self._live_reload_state()
            remap: dict[str, str] = {}
            if pairs:
                pred, pred_params = self._pairs_pred(pairs)
                remap = self._snapshot_id_remap(pred, pred_params, id_by_key)
            # Truncate this user's working-memory graph (edges first for the FK), then refill.
            self._conn.execute("DELETE FROM fact_edges WHERE org_id=%s AND user_id=%s", dst)
            self._conn.execute("DELETE FROM claims WHERE org_id=%s AND user_id=%s", dst)
            self._conn.execute("DELETE FROM facts WHERE org_id=%s AND user_id=%s", dst)
            if pairs:
                self._insert_snapshot_rows(pred, pred_params, remap)
                self._reapply_agent_meta(agent_meta_by_id)
        row = self._conn.execute(
            "SELECT count(*) FROM facts WHERE org_id=%s AND user_id=%s", dst
        ).fetchone()
        return int(row[0]) if row else 0

    def merge_caches_into_live(self, keys: list[tuple[str, str]]) -> int:
        """Additively upsert the given snapshots into this user's working memory.

        Unlike ``load_caches`` (which truncates the whole working graph first), this
        keeps every other live fact: for each fact in the selected snapshots it deletes
        the live fact it replaces and then inserts the snapshot rows + edges (stamped
        with the loader's ``user_id``). Returns facts inserted.

        The upsert is non-destructive of live agent state and stable in id. It runs in
        ONE transaction (the connection is autocommit, so an un-bracketed
        delete-then-insert would let a concurrent reader see the fact momentarily gone
        — a non-deterministic count). A requirement the working graph already holds is
        reconciled by natural key (``meta.requirement_id`` / ``req:`` tag): it keeps
        its LIVE fact id even when the snapshot stored the same requirement under a
        different id (so a logical requirement never changes id across a reload), and
        its live agent-owned meta (build lifecycle / lease / verification contract) is
        merged back OVER the snapshot baseline rather than reverted.
        """
        self._require_live("merge_caches_into_live")
        pairs = list(keys)
        if not pairs:
            return 0
        dst = (self.org_id, self.user_id)
        pred, pred_params = self._pairs_pred(pairs)
        with self._conn.transaction():
            id_by_key, agent_meta_by_id = self._live_reload_state()
            remap = self._snapshot_id_remap(pred, pred_params, id_by_key)
            # Live ids this reload replaces: the snapshot's own ids PLUS any live id a
            # natural-key match reconciles onto (the remap targets) — resolved to a
            # concrete list so the fact/edge deletes drop exactly the rows we reinsert.
            snap_ids = [
                r[0]
                for r in self._conn.execute(
                    f"SELECT id FROM snapshots WHERE org_id=%s AND {pred}",
                    (self.org_id, *pred_params),
                ).fetchall()
            ]
            replace_ids = list({*snap_ids, *remap.values()})
            if replace_ids:
                ph = ", ".join(["%s"] * len(replace_ids))
                # Drop existing live copies (edges first for the FK; claims cascade).
                self._conn.execute(
                    "DELETE FROM fact_edges WHERE org_id=%s AND user_id=%s "
                    f"AND (src_id IN ({ph}) OR dst_id IN ({ph}))",
                    (*dst, *replace_ids, *replace_ids),
                )
                self._conn.execute(
                    f"DELETE FROM facts WHERE org_id=%s AND user_id=%s AND id IN ({ph})",
                    (*dst, *replace_ids),
                )
            self._insert_snapshot_rows(pred, pred_params, remap)
            self._reapply_agent_meta(agent_meta_by_id)
        row = self._conn.execute(
            f"SELECT count(*) FROM snapshots WHERE org_id=%s AND {pred}",
            (self.org_id, *pred_params),
        ).fetchone()
        return int(row[0]) if row else 0

    def list_caches(self, space: str) -> list[dict]:
        """List the snapshots saved in ``space`` (org-shared), newest-first.

        Returns ``[{"snapshot", "count", "created_at"}]`` — one entry per distinct
        snapshot name in the space.
        """
        self._require_live("list_caches")
        rows = self._conn.execute(
            "SELECT snapshot, count(*), max(created_at) FROM snapshots "
            "WHERE org_id=%s AND space=%s "
            "GROUP BY snapshot ORDER BY max(created_at) DESC",
            (self.org_id, space),
        ).fetchall()
        return [
            {
                "snapshot": r[0],
                "count": int(r[1]),
                "created_at": r[2].isoformat() if hasattr(r[2], "isoformat") else str(r[2]),
            }
            for r in rows
        ]

    def delete_cache(self, space: str, snapshot: str) -> int:
        """Delete one org-shared snapshot (edges/claims cascade). Returns facts removed."""
        self._require_live("delete_cache")
        cur = self._conn.execute(
            "DELETE FROM snapshots WHERE org_id=%s AND space=%s AND snapshot=%s",
            (self.org_id, space, snapshot),
        )
        return cur.rowcount

    def rename_cache(self, space: str, old_snapshot: str, new_snapshot: str) -> int:
        """Rename a snapshot within ``space`` from ``old_snapshot`` to ``new_snapshot``.

        Rewrites ``snapshot`` across its facts, edges, and claims (all three carry it).
        Pure metadata update — embeddings/ids/states are untouched. Returns the number
        of fact rows moved (0 == ``old_snapshot`` had no rows). The caller is
        responsible for the collision check (``new_snapshot`` already in use) and for
        re-pointing any external references (e.g. ``mounted_snapshots``).
        """
        self._require_live("rename_cache")
        for table in ("snapshot_edges", "snapshot_claims", "snapshots"):
            self._conn.execute(
                f"UPDATE {table} SET snapshot=%s "
                "WHERE org_id=%s AND space=%s AND snapshot=%s",
                (new_snapshot, self.org_id, space, old_snapshot),
            )
        row = self._conn.execute(
            "SELECT count(*) FROM snapshots WHERE org_id=%s AND space=%s AND snapshot=%s",
            (self.org_id, space, new_snapshot),
        ).fetchone()
        return int(row[0]) if row else 0

    def cache_count(self, space: str, snapshot: str) -> int:
        """How many facts are in the snapshot ``(space, snapshot)`` (0 == absent)."""
        self._require_live("cache_count")
        row = self._conn.execute(
            "SELECT count(*) FROM snapshots WHERE org_id=%s AND space=%s AND snapshot=%s",
            (self.org_id, space, snapshot),
        ).fetchone()
        return int(row[0]) if row else 0

    # --- cross-org copy (share a snapshot between orgs) --------------------
    def copy_snapshot_from(
        self, src_org_id: str, src_space: str, src_snapshot: str
    ) -> int:
        """Copy another snapshot's rows INTO this snapshot-bound graph's ``(space, snapshot)``.

        Cross-org sharing: the source rows under ``(src_org_id, src_space,
        src_snapshot)`` are re-stamped with THIS graph's ``org_id`` and written under
        THIS graph's ``(space, snapshot)``. Ids, embeddings, state and meta are
        preserved verbatim (a pure SQL copy, no re-embedding). The caller authorizes
        both orgs and guarantees the destination snapshot is free. Returns facts copied.
        """
        if not self._is_snapshot:
            raise ValueError("copy_snapshot_from requires a snapshot-bound destination graph")
        dst_org = self.org_id
        dst = (self.space, self.snapshot)
        src = (src_org_id, src_space, src_snapshot)
        # WRITE-TIME SECTION INVARIANT: the DESTINATION snapshot name governs the kind, so a copy of a
        # mixed/validation source into a prd-* destination (or a plan source into a *-validation
        # destination) is refused — the same rule as save_cache, applied to a cross-org copy.
        violator = _snapshot_violator_sql(_snapshot_kind(self.snapshot))
        if violator is not None:
            row = self._conn.execute(
                "SELECT count(*) FROM snapshots "
                f"WHERE org_id=%s AND space=%s AND snapshot=%s AND ({violator})",
                src,
            ).fetchone()
            n = int(row[0]) if row else 0
            if n:
                raise SnapshotKindError(
                    f"{n} source fact(s) violate the {_snapshot_kind(self.snapshot)!r} kind of "
                    f"destination snapshot {self.snapshot!r}"
                )
        self._conn.execute(
            f"INSERT INTO snapshots ({_SNAPSHOT_COPY_COLS}, space, snapshot) "
            f"SELECT {_SNAPSHOT_RESTAMP_SELECT}, %s, %s FROM snapshots "
            "WHERE org_id=%s AND space=%s AND snapshot=%s",
            (dst_org, *dst, *src),
        )
        self._conn.execute(
            "INSERT INTO snapshot_edges (org_id, space, snapshot, src_id, dst_id, kind) "
            "SELECT %s, %s, %s, src_id, dst_id, kind FROM snapshot_edges "
            "WHERE org_id=%s AND space=%s AND snapshot=%s",
            (dst_org, *dst, *src),
        )
        self._conn.execute(
            f"INSERT INTO snapshot_claims ({_SNAPSHOT_CLAIM_COPY_COLS}, space, snapshot) "
            f"SELECT {_SNAPSHOT_CLAIM_RESTAMP_SELECT}, %s, %s FROM snapshot_claims "
            "WHERE org_id=%s AND space=%s AND snapshot=%s",
            (dst_org, *dst, *src),
        )
        row = self._conn.execute(
            "SELECT count(*) FROM snapshots WHERE org_id=%s AND space=%s AND snapshot=%s",
            (dst_org, *dst),
        ).fetchone()
        return int(row[0]) if row else 0

    def copy_live_from(self, src_org_id: str, src_user_id: str) -> int:
        """Copy another tenant's entire live graph into this (fresh) live graph.

        Cross-org sharing of a whole space: the source ``(src_org_id,
        src_user_id)`` facts/edges/claims are re-stamped with THIS graph's
        ``(org_id, user_id)`` and inserted. Intended for a freshly created,
        empty destination tenant (e.g. a brand-new space) — it does not truncate
        first, so a non-empty destination risks primary-key collisions on shared
        ids. The caller authorizes both tenants. Returns facts copied.
        """
        self._require_live("copy_live_from")
        dst = (self.org_id, self.user_id)
        src = (src_org_id, src_user_id)
        self._conn.execute(
            f"INSERT INTO facts ({_FACT_COPY_COLS}) "
            f"SELECT {_FACT_RESTAMP_SELECT} FROM facts WHERE org_id=%s AND user_id=%s",
            (*dst, *src),
        )
        self._conn.execute(
            "INSERT INTO fact_edges (org_id, user_id, src_id, dst_id, kind) "
            "SELECT %s, %s, src_id, dst_id, kind FROM fact_edges WHERE org_id=%s AND user_id=%s",
            (*dst, *src),
        )
        self._conn.execute(
            f"INSERT INTO claims ({_CLAIM_COPY_COLS}) "
            f"SELECT {_CLAIM_RESTAMP_SELECT} FROM claims WHERE org_id=%s AND user_id=%s",
            (*dst, *src),
        )
        row = self._conn.execute(
            "SELECT count(*) FROM facts WHERE org_id=%s AND user_id=%s", dst
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
        key_pred, params = self._key_pred("c")
        sql = (
            "SELECT c.subject, c.attribute, c.value, f.id, f.text, f.state "
            f"FROM {self._claims_table} c "
            f"JOIN {self._facts_table} f ON {self._join_keys('f', 'c')} AND f.id=c.fact_id "
            f"WHERE {key_pred} AND c.functional "
            "AND (c.subject, c.attribute) IN (SELECT unnest(%s::text[]), unnest(%s::text[]))"
        )
        params.extend([subjects, attributes])
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
        key_pred, key_params = self._key_pred()
        rows = self._conn.execute(
            "SELECT id, text, source, confidence, scope, category, observation_count, state "
            f"FROM {self._facts_table} WHERE {key_pred} "
            "AND state = 'active' "
            "ORDER BY created_at DESC LIMIT %s",
            (*key_params, limit),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def _add(self, decision: WriteDecision) -> str:
        # Human-approved adds enter at full credibility (confidence default 1.0).
        embedding = _fit(decision.embedding)  # reuse the vector from _recall
        fact_id = uuid.uuid4().hex
        meta = getattr(decision, "meta", None) or {}
        # WRITE-TIME SECTION INVARIANT (per-row, snapshot-bound writes only — the factory's
        # POST/PATCH into a snapshot target; MCP proxies through here too). Working-memory writes
        # (episodic and everything else) are unconstrained. ``_overwrite`` calls ``_add`` so it is
        # covered transitively.
        if self._is_snapshot:
            category = getattr(decision, "category", None)
            scope = meta.get("scope") or getattr(decision, "scope", None)
            if not _row_allowed(_snapshot_kind(self.snapshot), category, scope):
                raise SnapshotKindError(
                    f"a category={category!r} fact (scope={scope!r}) cannot be written into "
                    f"snapshot {self.snapshot!r} (kind {_snapshot_kind(self.snapshot)!r})"
                )
        # Bi-temporal validity: a new fact becomes valid now and stays valid
        # (invalid_at NULL) until something supersedes it. Callers may backdate
        # world-time validity by supplying meta["valid_at"] (ISO string or a
        # datetime); fall back to SQL now() otherwise.
        valid_at = meta.get("valid_at")
        if valid_at is None:
            valid_at = datetime.now(timezone.utc)
        # The partition key (org_id,user_id | org_id,space,snapshot) is stamped via
        # the key columns; snapshots carry no user_id and no outcome-trust columns.
        cols = ["id", *self._key_cols(), "text", "source", "confidence",
                "scope", "category", "state", "embedding",
                "valid_at", "invalid_at", "meta"]
        vals: list[object] = [
            fact_id,
            *self._key_vals(),
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
        placeholders = ", ".join(["%s"] * len(vals))
        self._conn.execute(
            f"INSERT INTO {self._facts_table} ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        self._persist_claims(fact_id, decision.claims)
        return fact_id

    def _merge(self, decision: WriteDecision) -> None:
        # Near-/exact-dup: bump the existing fact's evidence count, keep text.
        key_pred, key_params = self._key_pred()
        self._conn.execute(
            f"UPDATE {self._facts_table} SET observation_count = observation_count + 1, "
            "confidence = LEAST(1.0, COALESCE(confidence, 1.0) + 0.05) "
            f"WHERE {key_pred} AND id = %s",
            (*key_params, decision.update_target_id),
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
        key_pred, key_params = self._key_pred()
        self._conn.execute(
            f"UPDATE {self._facts_table} SET text = %s, embedding = %s, "
            "observation_count = observation_count + 1, "
            "confidence = LEAST(1.0, COALESCE(confidence, 1.0) + 0.05) "
            f"WHERE {key_pred} AND id = %s",
            (merged, _fit(self._embed(merged)), *key_params, decision.update_target_id),
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
            loser_pred, loser_params = self._key_pred("loser")
            self._conn.execute(
                f"UPDATE {self._facts_table} AS loser SET state = 'rejected', "
                "invalid_at = COALESCE("
                f"  (SELECT winner.valid_at FROM {self._facts_table} AS winner "
                f"    WHERE {self._join_keys('winner', 'loser')} AND winner.id = %s), "
                "  now()) "
                f"WHERE {loser_pred} AND loser.id = %s",
                (fact_id, *loser_params, loser_id),
            )
            src, dst = sorted((fact_id, loser_id))
            self.add_edge(src, dst, "contradicted_by")
            self._flag_stale_dependents(loser_id)  # H5: propagate to derived learnings
        return fact_id
