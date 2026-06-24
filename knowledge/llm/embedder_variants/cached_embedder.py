"""Replay real embedding vectors from a committed cache; record misses when allowed.

Embeddings are deterministic for a fixed ``(model, text)``, so we record real
vectors once (locally, with a key) and replay them everywhere else — letting CI
exercise real semantic ranking offline and deterministically. The cache key
includes the model id, so swapping models is a clean miss, never silent
staleness.

A miss with recording disabled is a **loud error**, not a silent fallback: it
means a seeded text or the embedding model changed without a refresh, which must
fail rather than pass on a stale fixture.

On-disk format: JSON mapping ``key -> base64(little-endian float32 bytes)``,
sorted keys for stable diffs. Packed float32 keeps the committed fixture compact
versus JSON float arrays.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import threading
from pathlib import Path

from knowledge.llm.atomic_write import atomic_write_text
from knowledge.llm.llm_def import Vector
from knowledge.llm.parent_embedder import Embedder

# Guards the read-modify-write in ``save`` so parallel cases (e.g. the runner's
# ``--workers``) recording to the *same* cache file can't clobber each other's new
# keys. Process-wide because the contention is between separate CachedEmbedder
# instances sharing one path, not within one instance.
_FILE_LOCK = threading.Lock()


def _pack(vec: Vector) -> str:
    return base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode("ascii")


def _unpack(blob: str) -> Vector:
    raw = base64.b64decode(blob)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


class CachedEmbedder(Embedder):
    """Serves vectors from a committed cache, recording misses only when allowed."""

    def __init__(
        self,
        inner: Embedder | None,
        cache_path: Path | str,
        *,
        model_id: str,
        allow_compute: bool,
    ) -> None:
        self.inner = inner
        self.cache_path = Path(cache_path)
        self.model_id = model_id
        self.allow_compute = allow_compute
        self._cache: dict[str, Vector] = self._load()
        self._dirty = False

    def embed(self, texts: list[str]) -> list[Vector]:
        misses = [t for t in texts if self._key(t) not in self._cache]
        if misses:
            if not (self.allow_compute and self.inner is not None):
                raise RuntimeError(
                    f"embedding cache miss for {len(misses)} text(s) under model "
                    f"{self.model_id!r} (e.g. {misses[0][:60]!r}). A seeded text or the "
                    "embedding model changed — refresh the cache locally with "
                    "OPENROUTER_API_KEY + OPENROUTER_EMBED_MODEL set "
                    "(`uv run python -m knowledge.evals.embed_cache --refresh`) and commit "
                    f"{self.cache_path.name}."
                )
            for text, vec in zip(misses, self.inner.embed(misses)):
                self._cache[self._key(text)] = vec
                self._dirty = True
            self.save()
        return [self._cache[self._key(t)] for t in texts]

    def save(self) -> None:
        """Write the cache back to disk (sorted, packed) if anything changed.

        Merges with the current on-disk state under a process-wide lock so a
        concurrent saver (parallel cases sharing this path) can't drop the other's
        newly recorded keys. Vectors are deterministic for a fixed ``(model, text)``,
        so merge precedence is immaterial.
        """
        if not self._dirty:
            return
        with _FILE_LOCK:
            merged = self._load()  # re-read: may include a peer's concurrent writes
            merged.update(self._cache)
            packed = {k: _pack(v) for k, v in merged.items()}
            atomic_write_text(
                self.cache_path, json.dumps(packed, indent=0, sort_keys=True) + "\n"
            )
            self._cache = merged  # adopt peers' keys so later hits resolve
            self._dirty = False

    def _load(self) -> dict[str, Vector]:
        if not self.cache_path.exists():
            return {}
        raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        return {k: _unpack(v) for k, v in raw.items()}

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model_id}\n{text}".encode("utf-8")).hexdigest()
