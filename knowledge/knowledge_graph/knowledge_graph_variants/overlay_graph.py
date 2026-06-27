"""Read-only overlay graph: live facts unioned with mounted snapshots.

A viewer can *mount* extra snapshots (their own or any org member's — see
:mod:`knowledge.serve.mounted_store`) so that retrieval reads expose the live
graph **plus** those snapshots, without merging them in. This wrapper is how that
union happens: it delegates everything to the live
:class:`~knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph.PostgresVectorGraph`
but composes the read methods (``search`` / ``read``) with a read-only vector
search of each mounted snapshot (``PostgresVectorGraph.search_cache``).

It is **read-only by use**: only the read routes construct it, and write/save
paths keep using the bare live graph — so a mounted overlay never participates in
an add and is never carried over when a snapshot is saved. Overlay hits are
tagged in ``fact.meta["mountedFrom"]`` (``{"userId", "snapshot"}``) so callers
can distinguish them from live facts.
"""

from __future__ import annotations

from typing import Any

from knowledge.knowledge_graph.knowledge_graph_def import Fact, SearchHit
from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
    _READ_CHAR_BUDGET,
    PostgresVectorGraph,
)
from knowledge.knowledge_graph.parent_searchable_graph import SearchableGraph


class OverlayGraph(SearchableGraph):
    """Compose a live graph's reads with read-only mounted-snapshot searches."""

    def __init__(
        self, live: PostgresVectorGraph, mounts: list[dict[str, str]]
    ) -> None:
        """``mounts`` is a list of ``{source_user_id, snapshot_name}`` dicts."""
        self._live = live
        # Normalize to (source_user_id, snapshot_name, cache_key) once.
        self._mounts = [
            (m["source_user_id"], m["snapshot_name"], f"snapshot:{m['snapshot_name']}")
            for m in mounts
        ]

    # --- SearchableGraph contract ------------------------------------------
    def search(self, query: str, *, top_k: int = 10, **kwargs: Any) -> list[SearchHit]:
        """Live + mounted snapshot hits, ranked by score (single UNION ALL query).

        Delegates to ``PostgresVectorGraph.overlay_search``, which embeds the query
        once and unions the live ``facts`` and ``cached_facts`` branches in one
        round trip. Mounted hits are tagged in ``fact.meta["mountedFrom"]``.
        """
        # Forward the optional read filters only when present, so callers/doubles that
        # don't take a kwarg are unaffected (the live overlay_search accepts them).
        # H2 exclusion plus the PR-#114 positive filters (category/scope/meta) — all
        # applied to BOTH the live and mounted branches by overlay_search.
        extra = {}
        for k in ("exclude_categories", "categories", "scope", "meta_filter"):
            if kwargs.get(k):
                extra[k] = kwargs[k]
        return self._live.overlay_search(
            query, [(u, key) for (u, _name, key) in self._mounts], top_k=top_k, **extra
        )

    # --- KnowledgeGraph read ----------------------------------------------
    def read(
        self,
        context: str | None = None,
        *,
        exclude_categories: list[str] | None = None,
        char_budget: int | None = None,
    ) -> str:
        """Concatenate retrieved fact text, mirroring ``PostgresVectorGraph.read``.

        Uses the composed ``search`` (with context) or recent active facts from
        the live graph and every mounted snapshot (without), budget-capped.
        ``exclude_categories`` (H2) omits those categories from the union.
        ``char_budget`` (gap H7) overrides the default context size cap per call.
        """
        context = (context or "").strip()
        excluded = set(exclude_categories or ())
        budget = _READ_CHAR_BUDGET if char_budget is None else char_budget
        if context:
            facts = [
                h.fact
                for h in self.search(context, top_k=12, exclude_categories=exclude_categories)
            ]
        else:
            facts = [f for f in self._recent_union(limit=50) if f.category not in excluded]
        parts: list[str] = []
        used = 0
        for fact in facts:
            if used + len(fact.text) > budget and parts:
                break
            parts.append(fact.text)
            used += len(fact.text)
        return "\n\n".join(parts)

    def _recent_union(self, limit: int) -> list[Fact]:
        """Recent active facts from the live graph plus every mounted snapshot."""
        seen: set[str] = set()
        out: list[Fact] = []
        for fact in self._live._recent(limit):
            if fact.id not in seen:
                seen.add(fact.id)
                out.append(fact)
        for source_user_id, snapshot_name, cache_key in self._mounts:
            for fact in self._live.recent_cache(
                source_user_id=source_user_id, cache_key=cache_key, limit=limit
            ):
                if fact.id in seen:
                    continue
                seen.add(fact.id)
                fact.meta["mountedFrom"] = {
                    "userId": source_user_id,
                    "snapshot": snapshot_name,
                }
                out.append(fact)
        return out[:limit]

    def write(self, *args: Any, **kwargs: Any) -> Any:
        """Overlay is read-only — writes must go through the bare live graph.

        Guards the invariant that a mounted overlay never participates in an add
        (and so is never carried over when a snapshot is saved). Write routes
        construct the live ``PostgresVectorGraph`` directly, never this wrapper.
        """
        raise NotImplementedError(
            "OverlayGraph is read-only; write through the live graph instead."
        )

    def __getattr__(self, name: str) -> Any:
        """Delegate any non-read method to the wrapped live graph."""
        return getattr(self._live, name)
