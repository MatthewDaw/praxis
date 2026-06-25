"""Shared core for structured single-call distillation ingestors.

``CommitIngestor`` and ``SessionIngestor`` are the same extractor reached from two
triggers (a merged PR vs. a solved-problem session). They emit the same typed
``{text, scope, category}`` fact via one constrained ``Llm.complete`` call and parse
it identically, precision-first (drop-malformed, keep well-formed siblings). The only
thing that genuinely differs is the distillation prompt's framing and the input label.

This module holds the parts that must not drift between the two:

- :data:`CATEGORIES` — the closed category set.
- :func:`build_distill_schema` — the strict ``json_schema`` ``response_format``,
  parameterized by name so each variant labels its schema without forking the shape.
- :class:`StructuredDistillIngestor` — the base implementing ``synthesis`` from three
  class attributes a variant sets: ``_DISTILL_PROMPT``, ``_INPUT_LABEL``, ``_SCHEMA_NAME``.
"""

from __future__ import annotations

import json

from knowledge.injestion.injestion_def import Insight
from knowledge.injestion.parent_injestor import Ingestor
from knowledge.knowledge_graph.parent_knowledge_graph import KnowledgeGraph
from knowledge.llm.llm_def import ChatMessage
from knowledge.llm.parent_llm import Llm

# Closed category set shared by every structured-distill variant.
CATEGORIES = ("decision", "gotcha", "convention", "rejected")


def build_distill_schema(name: str) -> dict:
    """A strict ``json_schema`` ``response_format`` for ``insights[].{text,scope,category}``.

    Returns a fresh dict on every call so a caller mutating the result cannot corrupt
    the schema for another variant. ``name`` labels the schema (e.g. ``pr_distillation``
    vs ``session_distillation``) — functionally inert to the provider, but it keeps the
    two variants' request payloads honestly distinct.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "insights": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "scope": {"type": "string"},
                                "category": {"type": "string", "enum": list(CATEGORIES)},
                            },
                            "required": ["text", "scope", "category"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["insights"],
                "additionalProperties": False,
            },
        },
    }


class StructuredDistillIngestor(Ingestor):
    """Distill a document into typed insights with one structured ``complete`` call.

    Subclasses set ``_DISTILL_PROMPT`` (the framing), ``_INPUT_LABEL`` (the heading the
    raw input is appended under), and ``_SCHEMA_NAME`` (the schema label). Everything
    else — the call shape, JSON parse, and precision-first drop-malformed handling — is
    shared here so the variants cannot diverge.
    """

    _DISTILL_PROMPT: str = ""
    _INPUT_LABEL: str = "UNIT INPUT"
    _SCHEMA_NAME: str = "distillation"

    def __init__(self, graph: KnowledgeGraph, llm: Llm) -> None:
        super().__init__(graph)
        self.llm = llm

    def synthesis(self, raw_input: str, *, source: str | None = None) -> list[Insight]:
        content = f"{self._DISTILL_PROMPT}\n\n{self._INPUT_LABEL}:\n{raw_input}"
        raw = self.llm.complete(
            [ChatMessage(role="user", content=content)],
            response_format=build_distill_schema(self._SCHEMA_NAME),
        )
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []  # non-JSON / empty reply -> nothing distilled (precision-first)
        insights: list[Insight] = []
        for item in data.get("insights", []) if isinstance(data, dict) else []:
            text = str(item.get("text", "")).strip()
            if not text:
                continue  # drop malformed entries, keep well-formed siblings
            scope = str(item.get("scope", "")).strip() or None
            category = str(item.get("category", "")).strip() or None
            insights.append(
                Insight(raw_text=text, source=source, scope=scope, category=category)
            )
        return insights
