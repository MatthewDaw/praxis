"""Committed record-once / replay-many cassettes for nondeterministic LLM output.

:class:`KeyedCassette` is the shared skeleton: record a real value once (locally, with
a key) and replay it everywhere else, so CI exercises the real merge/conflict/caption/
distill/embedding steps offline and deterministically. The key includes the model id,
so swapping models is a clean miss, never silent staleness. A miss with recording
disabled is a **loud error**, not a silent fallback: a changed seeded input or model
must fail rather than pass on a stale fixture. (Graceful "no judge/caption/cassette at
all" degradation is each caller's job — see ``MergeJudge``, ``image.captioner``,
``knowledge.evals.run``.)

Subclasses set the miss-error text and may add a value codec (embeddings) or extra key
material (the caption prompt); the sha256 key and the concurrent-merge save live here.

On-disk format: JSON mapping ``key -> encoded value``, sorted keys for stable diffs.
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


class KeyedCassette:
    """Serves keyed values from a committed JSON cassette, recording misses when allowed."""

    _key_prefix = ""  # extra key material after the model id (e.g. the caption prompt)

    def __init__(self, path: Path | str, *, model_id: str, allow_compute: bool) -> None:
        self.path = Path(path)
        self.model_id = model_id
        self.allow_compute = allow_compute
        self._cache: dict = self._load()
        self._dirty = False

    # Value codec: identity by default; overridden where the on-disk form differs.
    def _encode(self, value):
        return value

    def _decode(self, stored):
        return stored

    def _miss_error(self, payload: str) -> str:
        raise NotImplementedError

    def _record(self, payload: str, compute: Callable[[], object], *, can_compute: bool):
        """Replay ``payload`` from cache, or record ``compute()`` when ``can_compute``."""
        key = self._key(payload)
        if key in self._cache:
            return self._cache[key]
        if not can_compute:
            raise RuntimeError(self._miss_error(payload))
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
            packed = {k: self._encode(v) for k, v in merged.items()}
            atomic_write_text(self.path, json.dumps(packed, indent=0, sort_keys=True) + "\n")
            self._cache = merged
            self._dirty = False

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return {k: self._decode(v) for k, v in raw.items()}

    def _key(self, payload: str) -> str:
        material = f"{self.model_id}\n{self._key_prefix}{payload}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


class VerdictCassette(KeyedCassette):
    """Serves judge verdicts from a committed cassette, recording misses when allowed."""

    def verdict(self, payload: str, compute: Callable[[], dict]) -> dict:
        """Return the verdict for ``payload``, replaying or recording as allowed."""
        return self._record(payload, compute, can_compute=self.allow_compute)

    def _miss_error(self, payload: str) -> str:
        return (
            f"verdict cassette miss under model {self.model_id!r} "
            f"(payload e.g. {payload[:60]!r}). A seeded text or the judge model "
            "changed — refresh the cassette locally with OPENROUTER_API_KEY set "
            "(`uv run python -m knowledge.evals.verdict_cache --refresh`) and commit "
            f"{self.path.name}."
        )
