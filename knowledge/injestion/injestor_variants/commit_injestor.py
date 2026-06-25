"""Distill one merged PR (or commit) into Insight[] with a single structured call.

Unlike ``PromptIngestor`` (text-in/text-out ``LLM`` callable, post-split by line),
this variant needs the full ``Llm.complete(..., response_format=...)`` contract: it
emits a *typed* fact per PR — ``{text, scope, category}`` — in one constrained call.
The call shape mirrors ``dump_ingest._distill`` and ``ClaimExtractionJudge`` (one
structured ``complete`` -> ``json.loads`` -> drop-malformed, precision-first); it
deliberately does NOT adopt ``ingest_dump``'s slot-dedup / conflict reconciliation
(out of scope per R4).

``synthesis`` takes the rendered ``PRDocument`` text and the unit's ``source``
(``git/pr:<n>`` | ``git/commit:<sha>``), which it stamps onto every insight.
"""

from __future__ import annotations

import json

from knowledge.injestion.injestion_def import Insight
from knowledge.injestion.parent_injestor import Ingestor
from knowledge.knowledge_graph.parent_knowledge_graph import KnowledgeGraph
from knowledge.llm.llm_def import ChatMessage
from knowledge.llm.parent_llm import Llm

# R2: durable knowledge only; ignore churn. Scope from a small vocabulary, category
# from a closed set. Return no insights when nothing durable is present.
_DISTILL_PROMPT = (
    "You are distilling durable engineering knowledge from one merged pull request "
    "(or commit) for a coding agent that will work in this repository later.\n"
    "Extract ONLY knowledge that stays true after this change ships: design "
    "decisions and their rationale, gotchas / non-obvious constraints, conventions, "
    "and explicitly rejected approaches.\n"
    "IGNORE churn with no durable lesson: dependency/version bumps, pure renames, "
    "formatting, mechanical refactors, and anything restating WHAT changed without "
    "WHY it matters going forward.\n"
    "For each insight return:\n"
    "- text: one self-contained sentence or two, naming its own subject (no "
    "pronouns, no \"this PR\"), stating the durable fact and — for a decision or "
    "gotcha — why it holds. When the fact is a constraint on HOW code must be "
    "written — something an agent could violate without any error surfacing — fold "
    "in the concrete enforcement form (the guard, signature, or call) as a brief "
    "illustrative example, e.g. \"guard the delete with `AND state IN "
    "('proposed','rejected')`\", not just the rule in prose; frame it as an example "
    "so it stays directionally useful even if the surrounding code later changes.\n"
    "- scope: where the fact applies, as `file:<path>`, `module:<name>`, or `repo`.\n"
    "- category: one of decision | gotcha | convention | rejected.\n"
    "Prefer precision over recall: emit nothing rather than a vague or speculative "
    "fact. Return an empty list when the unit carries no durable knowledge."
)

_CATEGORIES = ("decision", "gotcha", "convention", "rejected")

_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "pr_distillation",
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
                            "category": {"type": "string", "enum": list(_CATEGORIES)},
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


class CommitIngestor(Ingestor):
    """Distill a PR/commit document into typed insights with one structured call."""

    def __init__(self, graph: KnowledgeGraph, llm: Llm) -> None:
        super().__init__(graph)
        self.llm = llm

    def synthesis(self, raw_input: str, *, source: str | None = None) -> list[Insight]:
        content = f"{_DISTILL_PROMPT}\n\nUNIT INPUT:\n{raw_input}"
        raw = self.llm.complete(
            [ChatMessage(role="user", content=content)], response_format=_SCHEMA
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
