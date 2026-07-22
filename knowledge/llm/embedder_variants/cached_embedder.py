"""Replay real embedding vectors from a committed cache; record misses when allowed.

A :class:`KeyedCassette` whose on-disk value is base64(little-endian float32 bytes) —
packed to keep the committed fixture compact versus JSON float arrays. Embeddings are
deterministic for a fixed ``(model, text)``, so recorded vectors replay everywhere and
merge precedence is immaterial. See ``verdict_cassette`` for the shared
record/replay/loud-miss contract. Unlike the single-value cassettes, ``embed`` is a
batch API, so the miss collection stays here.
"""

from __future__ import annotations

import base64
import struct

from knowledge.llm.llm_def import Vector
from knowledge.llm.parent_embedder import Embedder
from knowledge.llm.verdict_cassette import KeyedCassette


def _pack(vec: Vector) -> str:
    return base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode("ascii")


def _unpack(blob: str) -> Vector:
    raw = base64.b64decode(blob)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


class CachedEmbedder(Embedder, KeyedCassette):
    """Serves vectors from a committed cache, recording misses only when allowed."""

    def __init__(
        self,
        inner: Embedder | None,
        cache_path,
        *,
        model_id: str,
        allow_compute: bool,
    ) -> None:
        KeyedCassette.__init__(self, cache_path, model_id=model_id, allow_compute=allow_compute)
        self.inner = inner

    def _encode(self, value: Vector) -> str:
        return _pack(value)

    def _decode(self, stored: str) -> Vector:
        return _unpack(stored)

    def embed(self, texts: list[str]) -> list[Vector]:
        misses = [t for t in texts if self._key(t) not in self._cache]
        if misses:
            if not (self.allow_compute and self.inner is not None):
                raise RuntimeError(self._miss_error(misses[0], len(misses)))
            for text, vec in zip(misses, self.inner.embed(misses)):
                self._cache[self._key(text)] = vec
                self._dirty = True
            self.save()
        return [self._cache[self._key(t)] for t in texts]

    def _miss_error(self, sample: str, count: int = 1) -> str:
        return (
            f"embedding cache miss for {count} text(s) under model "
            f"{self.model_id!r} (e.g. {sample[:60]!r}). A seeded text or the "
            "embedding model changed — refresh the cache locally with "
            "OPENROUTER_API_KEY + OPENROUTER_EMBED_MODEL set "
            "(`uv run python -m knowledge.evals.embed_cache --refresh`) and commit "
            f"{self.path.name}."
        )
