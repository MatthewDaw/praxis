"""Tier-B (gated experiment): write-time controlled-vocabulary aspect tags.

The implicit-contradiction problem: two notes can contradict each other while
sharing almost no vocabulary and using no negation cue ("favor raw execution
speed" vs "keep code readable over micro-optimizations", ~0.45 cosine). Cosine
recall never surfaces them as candidates, so the conflict judge never sees the
pair. A *second*, non-similarity recall key is the only mechanism: tag each note
at write time with a controlled-vocabulary **aspect** (a tradeoff axis), then on
the conflict path also recall existing notes sharing a tag.

``AspectJudge`` mirrors ``MergeJudge``/``ConflictJudge``: structured output over
the LLM seam (constrained to :data:`ASPECT_VOCAB`), replayed offline from a
verdict cassette, graceful skip when no source. ``AspectTagger`` is the write step
that attaches the chosen tags to the decision (persisted to ``Fact.tags``).

This whole module is behind the FR-022 keep/kill gate — it is wired only into the
experiment's eval policy, never the production default, until the owner judges the
gate cleared.
"""

from __future__ import annotations

import json

from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision
from knowledge.llm.llm_def import ChatMessage
from knowledge.llm.parent_llm import Llm
from knowledge.llm.verdict_cassette import VerdictCassette

# Controlled vocabulary of *tradeoff axes*: both sides of a preference
# contradiction map to the same label even with disjoint surface vocabulary, so a
# same-tag recall can pair them. Kept small and seeded (not free-form) to limit
# fragmentation — the experiment's whole premise is shared assignment.
ASPECT_VOCAB = [
    "performance-vs-readability",
    "testing-rigor-vs-speed",
    "dependency-strategy",
    "logging-verbosity",
    "config-organization",
    "error-handling-strategy",
    "abstraction-vs-directness",
    "documentation-policy",
    "indentation-style",
    "comment-density",
]

_PROMPT = (
    "Assign zero or more aspect tags to the NOTE from the controlled vocabulary. "
    "An aspect is the underlying tradeoff axis the note takes a position on (e.g. "
    "a note favoring speed over clean code is about 'performance-vs-readability'). "
    "Pick only tags that clearly apply; return an empty list if none do.\n"
    "VOCABULARY: {vocab}\nNOTE: {note}"
)

# Structured output: the tags array is constrained to the controlled vocabulary.
_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "aspect_tags",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "tags": {"type": "array", "items": {"type": "string", "enum": ASPECT_VOCAB}}
            },
            "required": ["tags"],
            "additionalProperties": False,
        },
    },
}


class AspectJudge:
    """Assigns controlled-vocabulary aspect tags to a note (or None to skip)."""

    def __init__(
        self, llm: Llm | None = None, cassette: VerdictCassette | None = None
    ) -> None:
        self.llm = llm
        self.cassette = cassette

    def tags(self, note: str) -> list[str] | None:
        """Tags for ``note`` if a source is available; None to skip (no cassette, no llm)."""
        prompt = _PROMPT.format(vocab=", ".join(ASPECT_VOCAB), note=note)
        if self.cassette is not None:
            # Key on the rendered prompt so the vocabulary (carried in the prompt) is
            # part of the key, not just the note -- a vocab edit is a clean miss.
            return self.cassette.verdict(prompt, lambda: self._compute(prompt))["tags"]
        if self.llm is not None:
            return self._compute(prompt)["tags"]
        return None  # no source -> skip

    def _compute(self, prompt: str) -> dict:
        raw = self.llm.complete(
            [ChatMessage(role="user", content=prompt)],
            response_format=_SCHEMA,
        )
        tags = json.loads(raw)["tags"]
        # Defensive: keep only in-vocabulary tags (the schema already constrains a
        # compliant model; a cassette-recorded value is trusted as-is).
        return {"tags": [t for t in tags if t in ASPECT_VOCAB]}


class AspectTagger(WriteStep):
    """Attach controlled-vocab aspect tags to the incoming note (Tier-B, gated)."""

    consumes_candidates = False  # tags the new note; needs no existing candidates

    def __init__(self, judge: AspectJudge | None = None) -> None:
        self.judge = judge

    def apply(self, decision: WriteDecision) -> None:
        if self.judge is None or decision.dropped:
            return
        try:
            tags = self.judge.tags(decision.text)
        except Exception:
            # Tagging unavailable (e.g. no API key / network) — best-effort, leave
            # the note untagged rather than failing the write.
            return
        if tags:
            decision.tags = list(tags)
