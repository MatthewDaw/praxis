"""MVP ingestor: a hardcoded prompt + one LLM call.

``synthesis`` hands the raw input to an injected ``llm`` callable under a single
hardcoded :data:`SPLIT_PROMPT`, then splits the response into one
:class:`Insight` per non-empty line. The LLM is injected (rather than imported)
so the harness can run offline with a deterministic fake.

Its only job is to break raw input into discrete ideas — independent of what is
already in the graph. Reconciling against existing knowledge (dedup, conflict,
confidence) happens downstream in ``graph.write`` (see ``Ingestor.ingest``), so
``synthesis`` never reads the graph.

If no ``llm`` is provided, the raw input is treated as a single insight verbatim
— enough to exercise the loop without a live model.
"""

from __future__ import annotations

from typing import Callable

from knowledge.injestion.injestion_def import Insight
from knowledge.injestion.parent_injestor import Ingestor
from knowledge.knowledge_graph.parent_knowledge_graph import KnowledgeGraph

# The one hardcoded instruction for the MVP. Only the raw input is appended by
# ``synthesis`` before the call — no graph state, by design (reconciliation with
# existing knowledge is ``graph.write``'s job, not the splitter's).
SPLIT_PROMPT = (
    "Break the input into discrete, self-contained ideas worth remembering — one "
    "per line. Each line must be a single atomic fact or insight that stands on "
    "its own, preserving concrete specifics (names, numbers, projects). Do not "
    "merge distinct ideas, deduplicate, add commentary, or number the lines."
)

# An LLM is just a text-in/text-out callable here; keeps the dependency injectable.
LLM = Callable[[str], str]


class PromptIngestor(Ingestor):
    """Distill via a single prompted LLM call (or passthrough when no LLM)."""

    def __init__(self, graph: KnowledgeGraph, llm: LLM | None = None) -> None:
        super().__init__(graph)
        self.llm = llm

    def synthesis(self, raw_input: str) -> list[Insight]:
        # Split raw input into discrete ideas only — no graph read. Reconciling
        # with existing knowledge happens later in ``graph.write``.
        if self.llm is None:
            text = raw_input
        else:
            prompt = f"{SPLIT_PROMPT}\n\nINPUT:\n{raw_input}"
            text = self.llm(prompt)
        return [Insight(raw_text=line.strip()) for line in text.splitlines() if line.strip()]
