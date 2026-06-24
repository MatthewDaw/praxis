"""Replay VLM image captions from a committed cassette; record misses when allowed.

Captions are nondeterministic VLM output keyed exactly like embeddings and judge
verdicts, so we reuse the :class:`CachedEmbedder` / :class:`VerdictCassette`
pattern: record a real caption once (locally, with a key) and replay it
everywhere else, letting CI exercise real captions offline and deterministically.

The cache key includes the model id **and** the caption prompt text itself, so
swapping the caption model or editing the prompt is a clean miss — never silent
staleness, and with no version tag to remember to hand-bump.

A miss with recording disabled is a **loud error**, not a silent fallback: it
means an asset's canonical image or the caption model/prompt changed without a
refresh, which must fail rather than pass on a stale fixture. (Graceful "no
caption" degradation on a *live* VLM failure is the captioner's job, on the
no-cassette production path — see ``image.captioner``.)

On-disk format: JSON mapping ``key -> caption`` (sorted keys for stable diffs).
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Callable

from knowledge.llm.atomic_write import atomic_write_text

# Guards the read-modify-write in ``save`` so parallel cases recording to the same
# cassette can't clobber each other's keys.
_FILE_LOCK = threading.Lock()


class CaptionCassette:
    """Serves VLM captions from a committed cassette, recording misses when allowed."""

    def __init__(
        self, path: Path | str, *, model_id: str, prompt: str, allow_compute: bool
    ) -> None:
        self.path = Path(path)
        self.model_id = model_id
        self.prompt = prompt
        self.allow_compute = allow_compute
        self._cache: dict[str, str] = self._load()
        self._dirty = False

    def caption(self, payload: str, compute: Callable[[], str]) -> str:
        """Return the caption for ``payload`` (a content hash), replaying or recording."""
        key = self._key(payload)
        if key in self._cache:
            return self._cache[key]
        if not self.allow_compute:
            prompt_fp = hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()[:8]
            raise RuntimeError(
                f"caption cassette miss under model {self.model_id!r} / prompt#"
                f"{prompt_fp} (payload {payload[:16]!r}…). An asset's "
                "canonical image or the caption model/prompt changed — refresh the "
                "cassette locally with OPENROUTER_API_KEY set "
                "(`uv run python -m knowledge.evals.caption_cache --refresh`) and commit "
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

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _key(self, payload: str) -> str:
        return hashlib.sha256(
            f"{self.model_id}\n{self.prompt}\n{payload}".encode("utf-8")
        ).hexdigest()
