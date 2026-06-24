"""Replay LLM judge verdicts from a committed cassette; record misses when allowed.

The write-policy judges (merge / conflict) are nondeterministic and keyed exactly
like embeddings, so we reuse the :class:`CachedEmbedder` pattern one layer up: record
a real verdict once (locally, with a key) and replay it everywhere else — letting CI
exercise real merge/conflict decisions offline and deterministically. The cache key
includes the judge model id, so swapping the model is a clean miss, never silent
staleness.

A miss with recording disabled is a **loud error**, not a silent fallback: it means a
seeded text or the judge model changed without a refresh, which must fail rather than
pass on a stale fixture. (Graceful "no judge at all" degradation is the caller's job —
see ``MergeJudge``: with neither a cassette verdict nor a live LLM it skips.)

On-disk format: JSON mapping ``key -> verdict`` (a small JSON object), sorted keys for
stable diffs.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Callable

from knowledge.llm.atomic_write import atomic_write_text

# Guards the read-modify-write in ``save`` so parallel cases (the runner's
# ``--workers``) recording to the same cassette can't clobber each other's keys.
_FILE_LOCK = threading.Lock()


class VerdictCassette:
    """Serves judge verdicts from a committed cassette, recording misses when allowed."""

    def __init__(self, path: Path | str, *, model_id: str, allow_compute: bool) -> None:
        self.path = Path(path)
        self.model_id = model_id
        self.allow_compute = allow_compute
        self._cache: dict[str, dict] = self._load()
        self._dirty = False

    def verdict(self, payload: str, compute: Callable[[], dict]) -> dict:
        """Return the verdict for ``payload``, replaying or recording as allowed."""
        key = self._key(payload)
        if key in self._cache:
            return self._cache[key]
        if not self.allow_compute:
            raise RuntimeError(
                f"verdict cassette miss under model {self.model_id!r} "
                f"(payload e.g. {payload[:60]!r}). A seeded text or the judge model "
                "changed — refresh the cassette locally with OPENROUTER_API_KEY set "
                "(`uv run python -m knowledge.evals.verdict_cache --refresh`) and commit "
                f"{self.path.name}."
            )
        value = compute()
        self._cache[key] = value
        self._dirty = True
        self.save()
        return value

    def save(self) -> None:
        """Write the cassette back (sorted), merging on-disk state under a lock."""
        if not self._dirty:
            return
        with _FILE_LOCK:
            merged = self._load()  # re-read: may include a peer's concurrent writes
            merged.update(self._cache)
            atomic_write_text(self.path, json.dumps(merged, indent=0, sort_keys=True) + "\n")
            self._cache = merged
            self._dirty = False

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _key(self, payload: str) -> str:
        return hashlib.sha256(f"{self.model_id}\n{payload}".encode("utf-8")).hexdigest()
