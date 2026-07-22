"""Replay the ingestion splitter's distilled text from a committed cassette.

A :class:`KeyedCassette` for the ingestion LLM's ``raw input -> distilled text`` step,
exposed as the ``str -> str`` callable ``PromptIngestor`` expects. See
``verdict_cassette`` for the shared record/replay/loud-miss contract; graceful "no
cassette and no key" degradation is the caller's job (see ``knowledge.evals.run``,
which SKIPs a cassetted-ingestion case when neither a committed cassette nor a key is
available).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from knowledge.llm.verdict_cassette import KeyedCassette


class IngestionCassette(KeyedCassette):
    """Serves distilled ingestion text from a committed cassette, recording when allowed."""

    def __init__(
        self,
        path: Path | str,
        *,
        model_id: str,
        inner: Callable[[str], str] | None,
        allow_compute: bool,
    ) -> None:
        super().__init__(path, model_id=model_id, allow_compute=allow_compute)
        self.inner = inner

    def __call__(self, raw_input: str) -> str:
        """Return the distilled text for ``raw_input``, replaying or recording as allowed."""
        return self._record(
            raw_input,
            lambda: self.inner(raw_input),
            can_compute=self.allow_compute and self.inner is not None,
        )

    def _miss_error(self, payload: str) -> str:
        return (
            f"ingestion cassette miss under model {self.model_id!r} "
            f"(input e.g. {payload[:60]!r}). A seeded input or the ingest model "
            "changed — refresh the cassette locally with OPENROUTER_API_KEY set "
            "(`uv run python -m knowledge.evals.ingestion_cache --refresh`) and commit "
            f"{self.path.name}."
        )
