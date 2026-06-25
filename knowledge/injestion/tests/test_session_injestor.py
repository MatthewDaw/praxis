"""U2: offline tests for SessionIngestor — one structured LLM call -> Insight[].

Mirrors test_commit_injestor.py (scripted fake Llm + InMemoryGraph, no network),
keyed on the SESSION NARRATIVE: label. Covers R2 (structured distill -> typed
insights with source/scope/category) and R6 (the offline unit suite): durable-only
extraction, plus precision-first drop-malformed behavior.
"""

from __future__ import annotations

import json
from typing import Callable

from knowledge.injestion.injestor_variants.session_injestor import SessionIngestor
from knowledge.knowledge_graph.knowledge_graph_variants.in_memory_graph import (
    InMemoryGraph,
)
from knowledge.llm.llm_def import ChatMessage
from knowledge.llm.parent_llm import Llm


class FakeLlm(Llm):
    """Returns a canned reply (or one computed from the prompt); records calls."""

    def __init__(self, reply: str | Callable[[str], str]) -> None:
        self.reply = reply
        self.calls: list[tuple[list[ChatMessage], dict | None]] = []

    def complete(self, messages, *, temperature=0.0, max_tokens=1024, response_format=None):
        self.calls.append((messages, response_format))
        prompt = messages[-1].content
        return self.reply(prompt) if callable(self.reply) else self.reply


def _ingestor(reply):
    return SessionIngestor(InMemoryGraph(), FakeLlm(reply))


def test_two_insights_with_source_scope_category():
    # Covers R2: a structured two-insight reply -> two typed Insights, one LLM call.
    reply = json.dumps({"insights": [
        {"text": "Import knowledge lazily inside a yoyo step so the loader can resolve it.",
         "scope": "module:migrations", "category": "gotcha"},
        {"text": "Gate eval experiments on empirically-validated footguns.",
         "scope": "repo", "category": "convention"},
    ]})
    ing = _ingestor(reply)

    insights = ing.synthesis("PROBLEM: ...\nFIX: ...", source="session/abc123")

    assert len(insights) == 2
    assert all(i.source == "session/abc123" for i in insights)
    assert insights[0].scope == "module:migrations" and insights[0].category == "gotcha"
    assert insights[1].category == "convention"
    # Exactly one structured call (response_format passed) targeting the session schema.
    assert len(ing.llm.calls) == 1
    assert ing.llm.calls[0][1] is not None
    assert ing.llm.calls[0][1]["json_schema"]["name"] == "session_distillation"


def test_playbyplay_yields_no_insights_but_gotcha_does():
    # Covers R2/R6: a play-by-play-only narrative distills to []; a documented gotcha -> an insight.
    # Key on a token that appears only in the narrative input, never in the distill prompt.
    def reply(prompt: str) -> str:
        narrative = prompt.split("SESSION NARRATIVE:", 1)[-1]
        if "DURABLE-GOTCHA" in narrative:
            return json.dumps({"insights": [
                {"text": "yoyo execs migrations with the repo root off sys.path.",
                 "scope": "repo", "category": "gotcha"}]})
        return json.dumps({"insights": []})

    ing = _ingestor(reply)

    assert ing.synthesis("opened file X, then ran the test, it passed", source="session/1") == []
    out = ing.synthesis("DURABLE-GOTCHA the yoyo import gotcha", source="session/2")
    assert len(out) == 1 and out[0].category == "gotcha"


def test_malformed_entry_dropped_siblings_survive():
    # Edge: an entry missing `text` is dropped; the well-formed sibling survives, no raise.
    reply = json.dumps({"insights": [
        {"scope": "repo", "category": "gotcha"},  # malformed: no text
        {"text": "Real durable fact.", "scope": "repo", "category": "convention"},
    ]})

    out = _ingestor(reply).synthesis("narrative", source="session/3")

    assert [i.raw_text for i in out] == ["Real durable fact."]


def test_empty_response_yields_empty_not_crash():
    # Edge: an empty/whitespace or non-JSON reply -> [], not an exception.
    assert _ingestor("   ").synthesis("narrative", source="session/4") == []
    assert _ingestor("not json at all").synthesis("narrative", source="session/5") == []
