"""Persistent vector store of facts, backed by the RDS ``facts`` table.

The durable sibling of :class:`~knowledge.knowledge_graph.knowledge_graph_variants.vector_graph.VectorGraph`:
same ``KnowledgeGraph``/``SearchableGraph`` contract and the same write-policy
pipeline (redact -> dedup -> conflict), but every fact lives in Postgres under a
single ``(org_id, user_id)`` tenant with a pgvector embedding. Retrieval is a
pgvector cosine search; the read predicate ``org_id = %s AND (shared OR user_id
= %s)`` matches the rest of the multi-tenant schema (see ``serve/schema.sql``).

It reuses a connection the caller already holds (the backend opens one shared
autocommit connection per process and injects it), so this class never opens or
closes a connection itself. ``pgvector.psycopg`` is registered on that
connection (see ``serve/db.py``) so embeddings round-trip as plain python lists.
"""

from __future__ import annotations

import json
import uuid

import psycopg
from pgvector import Vector

from knowledge.knowledge_graph.knowledge_graph_def import Fact, SearchHit
from knowledge.knowledge_graph.parent_searchable_graph import SearchableGraph
from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision
from knowledge.knowledge_graph.write_policy.write_step_variants import (
    ConflictFlagger,
    ConflictJudge,
    Deduper,
    Redactor,
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

# Table names are interpolated directly into SQL (psycopg can't parametrize
# identifiers), so they must NEVER be user-controlled. Only these fixed names
# are permitted: the live-knowledge spine and the saved-state cache.
_ALLOWED_FACTS_TABLES = {"facts", "cached_facts"}
_ALLOWED_EDGES_TABLES = {"fact_edges", "cached_fact_edges"}

# Columns copied verbatim between `facts` and `cached_facts` for snapshot
# save/load (everything except the cache_key, which is stamped per copy).
_FACT_COPY_COLS = (
    "id, org_id, user_id, shared, text, source, confidence, scope, category, "
    "observation_count, state, embedding, meta, created_at"
)


def default_write_policy(llm: Llm | None = None) -> list[WriteStep]:
    """The baseline pipeline: redact, then dedup, then conflict-flag.

    Mirrors ``VectorGraph``'s default; the forced-overwrite add path injects a
    ``ConflictOverwriter`` policy instead.
    """
    return [Redactor(), Deduper(), ConflictFlagger(judge=ConflictJudge(llm=llm or OpenRouterLlm()))]


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

    # --- KnowledgeGraph contract -------------------------------------------
    def read(self, context: str | None = None) -> str:
        """Retrieve tenant knowledge: top-k similar to ``context``, else recent.

        Score-ordered and token-bounded so the result is a usable reader prompt.
        """
        context = (context or "").strip()
        if context:
            hits = self.search(context, top_k=12)
            facts = [h.fact for h in hits]
        else:
            facts = self._recent(limit=50)
        parts: list[str] = []
        used = 0
        for fact in facts:
            if used + len(fact.text) > _READ_CHAR_BUDGET and parts:
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
    ) -> str | None:
        """Run the write-policy pipeline over ``content``, then persist.

        ``state`` ("active" for a direct user approval, "proposed" for a passive
        system add) is the lifecycle state a freshly-added fact lands in.

        Returns the id of the added/updated/merged/overwritten fact, or ``None``
        if the write was dropped (empty or suppressed by a policy step).

        ``source``/``scope``/``category``/``meta`` are carried into persistence.
        Cache writes are stamped with this graph's bound ``cache_key``; ``meta``
        persists into the ``meta`` jsonb column.

        Side effect: for every existing fact the policy flagged as a
        contradiction (``decision.flags`` entries of the form
        ``"contradiction:<id>"``), a ``contradiction`` edge is inserted from the
        newly-persisted fact to that conflicting fact, so the dashboard
        Contradictions tab works directly off the facts spine.
        """
        content = content.strip()
        if not content:
            return None
        decision = WriteDecision(text=content, state="active" if state == "active" else "proposed")
        # Stash the persistence attributes on the decision so _add/_overwrite
        # (which read them off the decision via getattr) write them through.
        decision.source = source
        decision.scope = scope
        decision.category = category
        decision.meta = meta or {}
        for step in self.policy:
            if step.consumes_candidates and decision.embedding is None:
                self._recall(decision)  # embed once + one shared candidate pass
            step.apply(decision)
        if decision.dropped:
            return None
        if decision.embedding is None:
            # No candidate-consuming step ran; still embed once for persistence.
            decision.embedding = self._embed(decision.text)
        if decision.action == "update" and decision.update_target_id:
            self._merge(decision)
            fact_id = decision.update_target_id
        elif decision.action == "overwrite" and decision.update_target_id:
            self._overwrite(decision)
            fact_id = decision.update_target_id
        else:
            fact_id = self._add(decision)
        self._persist_contradictions(fact_id, decision)
        return fact_id

    def _persist_contradictions(self, fact_id: str, decision: WriteDecision) -> None:
        """Materialize the policy's ``contradiction:<id>`` flags as edges.

        ``ConflictFlagger`` records each detected conflict as a flag string
        ``"contradiction:<conflicting_fact_id>"`` on ``decision.flags`` (see
        ``write_step_variants/conflict_flagger.py``). We turn each into a
        persisted edge from the new fact to the conflicting one.
        """
        for flag in decision.flags:
            if not flag.startswith("contradiction:"):
                continue
            conflict_id = flag.split(":", 1)[1]
            if conflict_id and conflict_id != fact_id:
                self.add_edge(fact_id, conflict_id, "contradiction")

    # --- SearchableGraph contract ------------------------------------------
    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        filters: dict | None = None,
        scope: str | None = None,
        state: str | None = "active",
    ) -> list[SearchHit]:
        return self._search_vec(
            _fit(self._embed(query)),
            top_k=top_k,
            filters=filters,
            scope=scope,
            state=state,
        )

    def _search_vec(
        self,
        qvec: Vector,
        *,
        top_k: int = 10,
        filters: dict | None = None,
        scope: str | None = None,
        state: str | None = "active",
    ) -> list[SearchHit]:
        sql = (
            "SELECT id, text, source, confidence, scope, category, "
            "observation_count, state, 1 - (embedding <=> %s) AS score "
            f"FROM {self._facts_table} "
            "WHERE org_id = %s AND (shared OR user_id = %s) "
            "AND embedding IS NOT NULL"
        )
        params: list[object] = [qvec, self.org_id, self.user_id]
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
        sql += " ORDER BY embedding <=> %s LIMIT %s"
        params.extend([qvec, top_k])
        rows = self._conn.execute(sql, params).fetchall()
        return [
            SearchHit(
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
            for r in rows
        ]

    # --- dashboard snapshot (the graph the dashboard renders) --------------
    def active_facts(self) -> list[Fact]:
        """Every ``active`` fact for this tenant — the graph ``search`` reads.

        This is the one-to-one source for the dashboard graph view: the same
        rows ``read``/``search`` retrieve from, so what the dashboard shows is
        exactly what MCP ``get_context`` can recall (newest-first for display).
        """
        rows = self._conn.execute(
            "SELECT id, text, source, confidence, scope, category, observation_count, "
            "state, created_at, meta "
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

    # --- full-lifecycle reads (candidate-facade surface) -------------------
    @staticmethod
    def _row_to_fact(r: tuple) -> Fact:
        """Build a Fact from a row of (id,text,source,confidence,scope,category,
        observation_count,state[,created_at[,meta]]).

        ``created_at``/``meta`` are optional trailing columns; when present,
        ``created_at`` is serialized to ISO 8601 and ``meta`` is normalized to a
        dict (psycopg returns jsonb as a dict already, but tolerate strings).
        """
        created_at = None
        meta: dict = {}
        if len(r) > 8 and r[8] is not None:
            created_at = r[8].isoformat() if hasattr(r[8], "isoformat") else str(r[8])
        if len(r) > 9 and r[9] is not None:
            meta = r[9] if isinstance(r[9], dict) else json.loads(r[9])
        return Fact(
            id=r[0],
            text=r[1],
            source=r[2],
            confidence=r[3] if r[3] is not None else 1.0,
            scope=r[4],
            category=r[5],
            observation_count=r[6],
            state=r[7],
            created_at=created_at,
            meta=meta,
        )

    def all_facts(self, state: str | None = None) -> list[Fact]:
        """Every fact for this tenant (optionally filtered by ``state``), newest first."""
        sql = (
            "SELECT id, text, source, confidence, scope, category, observation_count, "
            "state, created_at, meta "
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
            "state, created_at, meta "
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

    def update_fact(
        self,
        fact_id: str,
        *,
        text: str | None = None,
        source: str | None = None,
        confidence: float | None = None,
        meta: dict | None = None,
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
        hits = self._search_vec(_fit(decision.embedding), top_k=self.recall_k, state=None)
        decision.candidates = [h for h in hits if h.score >= self.recall_floor]

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
        # cache_key only exists on cached_facts; meta exists on both tables.
        cols = ["id", "org_id", "user_id", "text", "source", "confidence",
                "scope", "category", "state", "embedding", "meta"]
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
        return fact_id

    def _merge(self, decision: WriteDecision) -> None:
        # Near-/exact-dup: bump the existing fact's evidence count, keep text.
        self._conn.execute(
            f"UPDATE {self._facts_table} SET observation_count = observation_count + 1, "
            "confidence = LEAST(1.0, COALESCE(confidence, 1.0) + 0.05) "
            "WHERE org_id = %s AND user_id = %s AND id = %s",
            (self.org_id, self.user_id, decision.update_target_id),
        )

    def _overwrite(self, decision: WriteDecision) -> None:
        # Forced upsert: the new approved truth replaces the nearest conflicting
        # fact in place (landing at the decision's state), then any other
        # contradictions decay, so no contradictory pair lingers.
        embedding = _fit(decision.embedding)  # reuse the vector from _recall
        meta = getattr(decision, "meta", None) or {}
        self._conn.execute(
            f"UPDATE {self._facts_table} SET text = %s, embedding = %s, source = %s, state = %s, "
            "meta = %s, confidence = 1.0, observation_count = observation_count + 1 "
            "WHERE org_id = %s AND user_id = %s AND id = %s",
            (
                decision.text,
                embedding,
                getattr(decision, "source", None),
                decision.state,
                json.dumps(meta),
                self.org_id,
                self.user_id,
                decision.update_target_id,
            ),
        )
        for sid in decision.supersede_ids:
            self._conn.execute(
                f"UPDATE {self._facts_table} SET state = 'decayed' "
                "WHERE org_id = %s AND user_id = %s AND id = %s",
                (self.org_id, self.user_id, sid),
            )
