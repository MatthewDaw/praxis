"""Replay VLM image captions from a committed cassette; record misses when allowed.

A :class:`KeyedCassette` whose key folds in the caption prompt as well as the model
id, so editing the prompt is a clean miss — never silent staleness, with no version
tag to hand-bump. See ``verdict_cassette`` for the shared record/replay/loud-miss
contract; graceful "no caption" degradation on a *live* VLM failure is the
captioner's job (see ``image.captioner``).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from knowledge.llm.verdict_cassette import KeyedCassette


class CaptionCassette(KeyedCassette):
    """Serves VLM captions from a committed cassette, recording misses when allowed."""

    def __init__(
        self, path: Path | str, *, model_id: str, prompt: str, allow_compute: bool
    ) -> None:
        super().__init__(path, model_id=model_id, allow_compute=allow_compute)
        self.prompt = prompt
        self._key_prefix = f"{prompt}\n"

    def caption(self, payload: str, compute: Callable[[], str]) -> str:
        """Return the caption for ``payload`` (a content hash), replaying or recording."""
        return self._record(payload, compute, can_compute=self.allow_compute)

    def _miss_error(self, payload: str) -> str:
        prompt_fp = hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()[:8]
        return (
            f"caption cassette miss under model {self.model_id!r} / prompt#"
            f"{prompt_fp} (payload {payload[:16]!r}…). An asset's "
            "canonical image or the caption model/prompt changed — refresh the "
            "cassette locally with OPENROUTER_API_KEY set "
            "(`uv run python -m knowledge.evals.caption_cache --refresh`) and commit "
            f"{self.path.name}."
        )
