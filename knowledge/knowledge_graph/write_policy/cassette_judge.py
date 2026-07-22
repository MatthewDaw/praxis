"""Shared base for the write-policy LLM judges.

Every judge over the LLM seam follows the same three-way dispatch: replay a
committed verdict from a ``VerdictCassette`` when one is injected, compute live
against an injected ``Llm``, or return ``None`` (no source -> the caller skips).
This base owns that plumbing plus the ``llm``/``cassette`` wiring and a one-call
structured-completion helper; each subclass supplies only its prompt(s), schema,
and the thin public method that renders the prompt and pulls its key out of the
verdict dict. The dict a subclass ``_compute`` returns is what the cassette stores,
so its shape must stay stable across refactors for recorded verdicts to replay.
"""

from __future__ import annotations

import json
from typing import Callable

from knowledge.llm.llm_def import ChatMessage
from knowledge.llm.parent_llm import Llm
from knowledge.llm.verdict_cassette import VerdictCassette


class CassetteJudge:
    """LLM verdict with deterministic cassette replay and graceful no-source skip."""

    def __init__(
        self, llm: Llm | None = None, cassette: VerdictCassette | None = None
    ) -> None:
        self.llm = llm
        self.cassette = cassette

    def _verdict(self, payload: str, compute: Callable[[], dict]) -> dict | None:
        """Cassette replay -> live compute -> None. ``payload`` keys the cassette, so a
        prompt or input edit is a clean miss (never a stale replay)."""
        if self.cassette is not None:
            return self.cassette.verdict(payload, compute)
        if self.llm is not None:
            return compute()
        return None

    def _complete_json(self, prompt: str, schema: dict) -> dict:
        """One structured LLM completion, parsed as JSON."""
        raw = self.llm.complete(
            [ChatMessage(role="user", content=prompt)], response_format=schema
        )
        return json.loads(raw)
