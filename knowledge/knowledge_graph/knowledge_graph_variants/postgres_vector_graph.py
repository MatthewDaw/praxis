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

import uuid

import psycopg
from pgvector import Vector

from knowledge.knowledge_graph.knowledge_graph_def import Fact, SearchHit
from knowledge.knowledge_graph.parent_searchable_graph import SearchableGraph
from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_policy_def import StoreView, WriteDecision
from knowledge.knowledge_graph.write_policy.write_step_variants import (
    ConflictFlagger,
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


def default_write_policy(llm: Llm | None = None) -> list[WriteStep]:
    """The baseline pipeline: redact, then dedup, then conflict-flag.

    Mirrors ``VectorGraph``'s default; the forced-overwrite add path injects a
    ``ConflictOverwriter`` policy instead.
    """
    return [Redactor(), Deduper(), ConflictFlagger(llm=llm or OpenRouterLlm())]


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
    ) -> None:
        self._conn = conn
        self.org_id = org_id
        self.user_id = user_id
        # Real defaults for production; tests inject deterministic fakes.
        self.embedder = embedder or OpenRouterEmbedder()
        self.policy = policy if policy is not None else default_write_policy()

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

    def write(self, content: str) -> None:
        """Run the write-policy pipeline over ``content``, then persist."""
        content = content.strip()
        if not content:
            return
        decision = WriteDecision(text=content)
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
        qvec = _fit(self._embed(query))
        sql = (
            "SELECT id, text, source, confidence, scope, category, "
            "observation_count, 1 - (embedding <=> %s) AS score "
            "FROM facts "
            "WHERE org_id = %s AND (shared OR user_id = %s) "
            "AND embedding IS NOT NULL"
        )
        params: list[object] = [qvec, self.org_id, self.user_id]
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
                ),
                score=float(r[7]),
            )
            for r in rows
        ]

    # --- StoreView (used by write steps) -----------------------------------
    def most_similar(self, text: str, k: int = 5) -> list[SearchHit]:
        return self.search(text, top_k=k)

    # --- internals ----------------------------------------------------------
    def _embed(self, text: str) -> list[float]:
        with tracing.llm_span("embed", kind="EMBEDDING", input_value=text) as span:
            vec = self.embedder.embed_one(text)
            tracing.record_output(span, output=f"<{len(vec)}-dim vector>")
        return vec

    def _recent(self, limit: int) -> list[Fact]:
        rows = self._conn.execute(
            "SELECT id, text, source, confidence, scope, category, observation_count "
            "FROM facts WHERE org_id = %s AND (shared OR user_id = %s) "
            "ORDER BY created_at DESC LIMIT %s",
            (self.org_id, self.user_id, limit),
        ).fetchall()
        return [
            Fact(
                id=r[0],
                text=r[1],
                source=r[2],
                confidence=r[3] if r[3] is not None else 1.0,
                scope=r[4],
                category=r[5],
                observation_count=r[6],
            )
            for r in rows
        ]

    def _add(self, decision: WriteDecision) -> None:
        # Human-approved adds enter at full credibility (confidence default 1.0).
        embedding = _fit(self._embed(decision.text))
        self._conn.execute(
            "INSERT INTO facts (id, org_id, user_id, text, source, confidence, "
            "scope, category, embedding) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                uuid.uuid4().hex,
                self.org_id,
                self.user_id,
                decision.text,
                getattr(decision, "source", None),
                1.0,
                getattr(decision, "scope", None),
                getattr(decision, "category", None),
                embedding,
            ),
        )

    def _merge(self, decision: WriteDecision) -> None:
        # Near-/exact-dup: bump the existing fact's evidence count, keep text.
        self._conn.execute(
            "UPDATE facts SET observation_count = observation_count + 1, "
            "confidence = LEAST(1.0, COALESCE(confidence, 1.0) + 0.05) "
            "WHERE org_id = %s AND user_id = %s AND id = %s",
            (self.org_id, self.user_id, decision.update_target_id),
        )

    def _overwrite(self, decision: WriteDecision) -> None:
        # Forced upsert: the new approved truth replaces the nearest conflicting
        # fact in place, then any other contradictions are deleted (their edges
        # cascade), so no contradictory pair lingers.
        embedding = _fit(self._embed(decision.text))
        self._conn.execute(
            "UPDATE facts SET text = %s, embedding = %s, source = %s, "
            "confidence = 1.0, observation_count = observation_count + 1 "
            "WHERE org_id = %s AND user_id = %s AND id = %s",
            (
                decision.text,
                embedding,
                getattr(decision, "source", None),
                self.org_id,
                self.user_id,
                decision.update_target_id,
            ),
        )
        for sid in decision.supersede_ids:
            self._conn.execute(
                "DELETE FROM facts WHERE org_id = %s AND user_id = %s AND id = %s",
                (self.org_id, self.user_id, sid),
            )
