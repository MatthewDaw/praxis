"""Replay the ingestion splitter's distilled text from a committed cassette.

The ingestion LLM's ``raw input -> distilled text`` step is keyed exactly like
embeddings and judge verdicts, so we reuse the :class:`CachedEmbedder` pattern one
layer up: record the real distilled output once (locally, with a key) and replay it
everywhere else — letting CI exercise real distillation offline and
deterministically. The cache key includes the ingest model id, so swapping the model
is a clean miss, never silent staleness.

A miss with recording disabled is a **loud error**, not a silent fallback: it means a
seeded input or the ingest model changed without a refresh, which must fail rather
than pass on a stale fixture. (Graceful "no cassette and no key" degradation is the
caller's job — see ``knowledge.evals.run``: a case requiring cassetted ingestion
SKIPs when neither a committed cassette nor a key is available.)

On-disk format: JSON mapping ``key -> distilled_text``, sorted keys for stable diffs.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Callable

# Guards the read-modify-write in ``save`` so parallel cases (the runner's
# ``--workers``) recording to the same cassette can't clobber each other's keys.
_FILE_LOCK = threading.Lock()


class IngestionCassette:
    """Serves distilled ingestion text from a committed cassette, recording when allowed.

    A ``str -> str`` callable (the shape ``PromptIngestor``'s LLM expects): given the
    raw ingestion input, return the recorded distilled text, or — only with a key and
    a live ``inner`` — compute, record, and persist it.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        model_id: str,
        inner: Callable[[str], str] | None,
        allow_compute: bool,
    ) -> None:
        self.path = Path(path)
        self.model_id = model_id
        self.inner = inner
        self.allow_compute = allow_compute
        self._cache: dict[str, str] = self._load()
        self._dirty = False

    def __call__(self, raw_input: str) -> str:
        """Return the distilled text for ``raw_input``, replaying or recording as allowed."""
        key = self._key(raw_input)
        if key in self._cache:
            return self._cache[key]
        if not (self.allow_compute and self.inner is not None):
            raise RuntimeError(
                f"ingestion cassette miss under model {self.model_id!r} "
                f"(input e.g. {raw_input[:60]!r}). A seeded input or the ingest model "
                "changed — refresh the cassette locally with OPENROUTER_API_KEY set "
                "(`uv run python -m knowledge.evals.ingestion_cache --refresh`) and commit "
                f"{self.path.name}."
            )
        value = self.inner(raw_input)
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
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(merged, indent=0, sort_keys=True) + "\n", encoding="utf-8"
            )
            self._cache = merged
            self._dirty = False

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _key(self, raw_input: str) -> str:
        return hashlib.sha256(f"{self.model_id}\n{raw_input}".encode("utf-8")).hexdigest()
