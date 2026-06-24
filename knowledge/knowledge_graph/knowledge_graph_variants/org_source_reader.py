"""Read-only view of another org member's facts, for skill sharing / fold-in.

The live :class:`PostgresVectorGraph` is bound to a single ``(org_id, user_id)``
tenant and gates every read with ``org_id = %s AND (shared OR user_id = %s)`` —
a member can never see another member's *private* facts through it. Skill
sharing needs exactly that: within an org (the trust boundary), any member may
browse any other member's saved snapshot and cherry-pick facts to fold into
their own graph.

This class is the read path for that. It is deliberately **read-only and
write-free**: it owns no ``write``/``add``/``delete``/cache-mutation method, so
the predicate relaxation (``org_id = %s AND user_id = %s`` for an explicit target
user, no ``shared`` clause) can never leak into a mutation. Authorization —
"the caller is a member of this org" — is enforced one layer up by the route's
``active_org`` dependency; this class only pins ``org_id`` so a reader scoped to
org X never observes any other org's rows.

Sources are snapshots only: ``cache_key`` is required (always a
``"snapshot:<name>"`` key) and reads the target user's saved state in
``cached_facts``/``cached_fact_edges``. The live graph is never browsed.
"""

from __future__ import annotations

import psycopg

from knowledge.knowledge_graph.knowledge_graph_def import Fact
from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
    PostgresVectorGraph,
)

# Same fixed-name allowlist discipline as PostgresVectorGraph: table names are
# interpolated into SQL (psycopg can't parametrize identifiers), so they are
# chosen here from constants and never user-controlled.
_CACHE_FACTS, _CACHE_EDGES = "cached_facts", "cached_fact_edges"

_FACT_COLS = (
    "id, text, source, confidence, scope, category, observation_count, "
    "state, created_at, meta, cluster_id, cluster_label"
)


class OrgSourceReader:
    """Read-only access to one org member's snapshot facts/edges."""

    def __init__(
        self,
        conn: psycopg.Connection,
        org_id: str,
        target_user_id: str,
        *,
        cache_key: str,
    ) -> None:
        self._conn = conn
        self.org_id = org_id
        self.target_user_id = target_user_id
        self._cache_key = cache_key

    def _where(self) -> tuple[str, list[object]]:
        """The org+target-user+snapshot predicate."""
        return (
            "org_id = %s AND user_id = %s AND cache_key = %s",
            [self.org_id, self.target_user_id, self._cache_key],
        )

    def all_facts(self, state: str | None = None) -> list[Fact]:
        """Every fact in the snapshot (optionally filtered by ``state``), newest first."""
        where, params = self._where()
        sql = f"SELECT {_FACT_COLS} FROM {_CACHE_FACTS} WHERE {where}"
        if state is not None:
            sql += " AND state = %s"
            params.append(state)
        sql += " ORDER BY created_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [PostgresVectorGraph._row_to_fact(r) for r in rows]

    def get_facts(self, fact_ids: list[str]) -> list[Fact]:
        """Fetch the named facts from the snapshot (order not guaranteed)."""
        ids = list(fact_ids)
        if not ids:
            return []
        where, params = self._where()
        sql = f"SELECT {_FACT_COLS} FROM {_CACHE_FACTS} WHERE {where} AND id = ANY(%s)"
        params.append(ids)
        rows = self._conn.execute(sql, params).fetchall()
        return [PostgresVectorGraph._row_to_fact(r) for r in rows]

    def edges_among(self, fact_ids: list[str]) -> list[tuple[str, str, str]]:
        """``(src, dst, kind)`` edges whose *both* endpoints are in ``fact_ids``.

        Edges touching a fact outside the selection are dropped — fold-in only
        carries an edge when both of its facts come along (see plan KTD4).
        """
        ids = list(fact_ids)
        if not ids:
            return []
        where, params = self._where()
        sql = (
            f"SELECT src_id, dst_id, kind FROM {_CACHE_EDGES} "
            f"WHERE {where} AND src_id = ANY(%s) AND dst_id = ANY(%s)"
        )
        params.extend([ids, ids])
        rows = self._conn.execute(sql, params).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]
