"""U1: tests for the shared structured-distill core.

Covers the base class via a throwaway subclass (so the shared parse loop has coverage
independent of CommitIngestor/SessionIngestor) and the schema-name factory. The
behavior-preservation contract for CommitIngestor lives in test_commit_injestor.py,
which must pass unchanged after the refactor.
"""

from __future__ import annotations

import json
from typing import Callable

from knowledge.injestion.injestor_variants.structured_distill import (
    CATEGORIES,
    StructuredDistillIngestor,
    build_distill_schema,
)
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


class _ProbeIngestor(StructuredDistillIngestor):
    _DISTILL_PROMPT = "Distill the probe input."
    _INPUT_LABEL = "PROBE INPUT"
    _SCHEMA_NAME = "probe_distillation"


def test_base_parses_structured_reply_into_typed_insights():
    # The shared synthesis turns a structured reply into typed Insights in one call.
    reply = json.dumps({"insights": [
        {"text": "Probe lesson one.", "scope": "repo", "category": "convention"},
        {"text": "Probe lesson two.", "scope": "file:x.py", "category": "gotcha"},
    ]})
    ing = _ProbeIngestor(InMemoryGraph(), FakeLlm(reply))

    insights = ing.synthesis("raw probe text", source="probe/1")

    assert [i.raw_text for i in insights] == ["Probe lesson one.", "Probe lesson two."]
    assert all(i.source == "probe/1" for i in insights)
    assert insights[0].category == "convention" and insights[1].scope == "file:x.py"
    # Exactly one structured call (response_format passed).
    assert len(ing.llm.calls) == 1
    assert ing.llm.calls[0][1] is not None


def test_base_uses_input_label_and_schema_name():
    # The subclass's input label and schema name flow into the call verbatim.
    captured: dict = {}

    def reply(prompt: str) -> str:
        captured["prompt"] = prompt
        return json.dumps({"insights": []})

    ing = _ProbeIngestor(InMemoryGraph(), FakeLlm(reply))
    ing.synthesis("body text", source="probe/2")

    assert "PROBE INPUT:\nbody text" in captured["prompt"]
    assert ing.llm.calls[0][1]["json_schema"]["name"] == "probe_distillation"


def test_base_drops_malformed_and_handles_non_json():
    # Malformed entry dropped, sibling survives; non-JSON / empty -> [].
    reply = json.dumps({"insights": [
        {"scope": "repo", "category": "gotcha"},  # malformed: no text
        {"text": "Real fact.", "scope": "repo", "category": "decision"},
    ]})
    assert [i.raw_text for i in _ProbeIngestor(InMemoryGraph(), FakeLlm(reply)).synthesis("d")] == [
        "Real fact.",
    ]
    assert _ProbeIngestor(InMemoryGraph(), FakeLlm("   ")).synthesis("d") == []
    assert _ProbeIngestor(InMemoryGraph(), FakeLlm("not json")).synthesis("d") == []


def test_build_distill_schema_names_and_returns_fresh_dict():
    # Name is reflected; each call returns an independent dict (mutation-safe).
    a = build_distill_schema("alpha")
    b = build_distill_schema("alpha")
    assert a["json_schema"]["name"] == "alpha"
    assert a is not b
    a["json_schema"]["name"] = "mutated"
    assert b["json_schema"]["name"] == "alpha"
    # Category enum tracks the shared closed set.
    enum = a["json_schema"]["schema"]["properties"]["insights"]["items"]["properties"][
        "category"
    ]["enum"]
    assert tuple(enum) == CATEGORIES
