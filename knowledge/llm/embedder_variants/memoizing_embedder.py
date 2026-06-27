"""In-process memoizing embedder for collapsing per-write embedding round-trips.

The per-insight write path embeds the insight text once via ``embed_one``. When a
bulk batch writes N insights serially, that is N separate embedding HTTP calls.
Wrapping the real embedder in this memo and pre-embedding the whole batch up front
(one ``embed`` call -> one ``{"input": [...]}`` round-trip) turns those N calls
into one, while every later ``embed_one`` resolves from the memo.

This changes only *how many* network calls are made, never *what* gets embedded:
any text not pre-warmed (e.g. a policy step embedding derived text) simply misses
the memo and falls through to the inner embedder, exactly as before. The memo is
per-instance and intended to live for one batch request, so it never grows
unbounded across requests.

The memo is guarded by a lock so the parallel batch writer can share one instance
across worker threads (each running ``decide`` -> ``embed_one``) without racing on
the dict. After a prefetch the workers only read hits, but the lock keeps a stray
miss (an un-prewarmed text) from corrupting the dict under concurrency.
"""

from __future__ import annotations

import threading

from knowledge.llm.llm_def import Vector
from knowledge.llm.parent_embedder import Embedder


class MemoizingEmbedder(Embedder):
    """Wraps an embedder with an in-process, lock-guarded memo keyed on exact text."""

    def __init__(self, inner: Embedder) -> None:
        self.inner = inner
        self._memo: dict[str, Vector] = {}
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[Vector]:
        with self._lock:
            misses = [t for t in texts if t not in self._memo]
        if misses:
            # Dedup misses (order-preserved) so repeats cost one inner vector, and
            # so a single inner call covers the whole batch. The inner call runs
            # outside the lock (it may be a slow network round-trip); concurrent
            # callers may redundantly embed the same miss, but the stored vector is
            # deterministic for a fixed (model, text), so last-writer-wins is safe.
            unique = list(dict.fromkeys(misses))
            embedded = dict(zip(unique, self.inner.embed(unique)))
            with self._lock:
                self._memo.update(embedded)
        with self._lock:
            return [self._memo[t] for t in texts]

    def prefetch(self, texts: list[str]) -> None:
        """Warm the memo for ``texts`` in one inner batch call (result discarded)."""
        wanted = [t for t in texts if t and t not in self._memo]
        if wanted:
            self.embed(wanted)
