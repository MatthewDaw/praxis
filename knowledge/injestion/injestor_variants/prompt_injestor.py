"""MVP ingestor: a hardcoded prompt + one LLM call.

``synthesis`` hands the raw input to an injected ``llm`` callable under a single
hardcoded :data:`UPDATE_PROMPT`, then splits the response into one
:class:`Insight` per non-empty line. The LLM is injected (rather than imported)
so the harness can run offline with a deterministic fake.

If no ``llm`` is provided, the raw input is treated as a single insight verbatim
— enough to exercise the loop without a live model.
"""

from __future__ import annotations

from typing import Callable

from knowledge.injestion.injestion_def import Insight
from knowledge.injestion.parent_injestor import Ingestor
from knowledge.knowledge_graph.parent_knowledge_graph import KnowledgeGraph

# The one hardcoded instruction for the MVP. The current graph + raw input are
# appended by ``synthesis`` before the call.
UPDATE_PROMPT = (
    "You maintain a knowledge base for a coding agent. Given the current "
    "knowledge and a new observation, return the distilled insights worth "
    "remembering — one per line, terse and general. Omit anything already "
    "captured or too specific to reuse."
)

# An LLM is just a text-in/text-out callable here; keeps the dependency injectable.
LLM = Callable[[str], str]


class PromptIngestor(Ingestor):
    """Distill via a single prompted LLM call (or passthrough when no LLM)."""

    def __init__(self, graph: KnowledgeGraph, llm: LLM | None = None) -> None:
        super().__init__(graph)
        self.llm = llm

    def synthesis(self, raw_input: str) -> list[Insight]:
        if self.llm is None:
            text = raw_input
        else:
            prompt = f"{UPDATE_PROMPT}\n\nCURRENT KNOWLEDGE:\n{self.graph.read()}\n\nNEW OBSERVATION:\n{raw_input}"
            text = self.llm(prompt)
        return [Insight(raw_text=line.strip()) for line in text.splitlines() if line.strip()]
