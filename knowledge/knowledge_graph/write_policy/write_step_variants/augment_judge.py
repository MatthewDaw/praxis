"""LLM augment-judge: should a new note be MERGED INTO an existing fact (Mem0 UPDATE)?

The precision arbiter for the additive-merge path. Given the incoming note and an
existing candidate fact, it asks an injected ``Llm`` whether the two are about the
same subject and *additive* (the new note adds detail to / extends the existing
fact without contradicting it) and, if so, for a single synthesized merged
sentence that preserves both. Unlike the ``MergeJudge`` (which only selects an
existing verbatim survivor), this judge *rewrites*: the merged text is the new
survivor, so the verdict carries ``merge`` plus the synthesized ``merged_text``.

Determinism + graceful degradation, mirroring ``MergeJudge``:
- a ``VerdictCassette`` replays committed verdicts offline (loud-miss on a stale one);
- with a live ``llm`` and no cassette, it computes directly (production path);
- with neither, ``merged_text`` returns ``None`` — the caller skips the augment
  (the write falls through to add/conflict unchanged).
"""

from __future__ import annotations

import json

from knowledge.llm.llm_def import ChatMessage
from knowledge.llm.parent_llm import Llm
from knowledge.llm.verdict_cassette import VerdictCassette

_PROMPT = (
    "You maintain a memory store. Decide whether the NEW note should be MERGED INTO "
    "the existing one. Merge them when they describe the SAME subject and are "
    "ADDITIVE — the new note adds or lists further detail (another preference, item, "
    "attribute, or example) about that subject WITHOUT contradicting the existing "
    "note. Differences in wording or intensity (e.g. 'likes' vs 'loves') do NOT block "
    "a merge; only a genuine factual conflict does.\n"
    "Do NOT merge two distinct RULES, POLICIES, or DEFINITIONS that govern DIFFERENT "
    "subjects, even when they share key phrases — folding them into one fact destroys "
    "each one's separate identity (each must stay independently citable). A shared term "
    "is not the same subject; ask what THING each statement is primarily about.\n"
    "Examples that SHOULD merge (same subject, additive):\n"
    "  EXISTING 'The user likes cheese pizza.' NEW 'The user loves chicken pizza.' "
    "-> 'The user likes cheese and chicken pizza.'\n"
    "Examples that should NOT merge:\n"
    "  EXISTING 'Lives in Paris.' NEW 'Works as a chef.' -> different subjects.\n"
    "  EXISTING 'A requirement is admitted to a plan only if it carries a binary "
    "acceptance condition.' NEW 'A plan is complete only when every requirement has a "
    "binary acceptance condition and no contradiction remains unresolved.' -> two "
    "distinct rules (an admission gate about a requirement vs a completion gate about a "
    "plan) that merely share the phrase 'binary acceptance condition'; keep them "
    "separate.\n"
    "Set merge true ONLY when same subject AND additive (not contradictory, not "
    "unrelated, not two distinct rules, not already identical). When merge is true, set "
    "merged_text to a SINGLE sentence preserving the information from BOTH notes; "
    "otherwise set merged_text to an empty string.\n"
    "EXISTING: {existing}\nNEW: {new}"
)

_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "augment_verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "merge": {"type": "boolean"},
                "merged_text": {"type": "string"},
            },
            "required": ["merge", "merged_text"],
            "additionalProperties": False,
        },
    },
}


class AugmentJudge:
    """Decides whether an incoming note should be folded into an existing one."""

    def __init__(
        self, llm: Llm | None = None, cassette: VerdictCassette | None = None
    ) -> None:
        self.llm = llm
        self.cassette = cassette

    def merged_text(self, incoming: str, existing: str) -> str | None:
        """The synthesized merged sentence if the note is additive; None to skip.

        Returns ``None`` both when there's no verdict source (no cassette, no llm)
        and when the judge ruled the pair non-additive (``merge`` false) — either
        way the caller leaves the write as a plain add.
        """
        prompt = _PROMPT.format(existing=existing, new=incoming)
        if self.cassette is not None:
            verdict = self.cassette.verdict(prompt, lambda: self._compute(prompt))
        elif self.llm is not None:
            verdict = self._compute(prompt)
        else:
            return None
        if not verdict.get("merge"):
            return None
        text = (verdict.get("merged_text") or "").strip()
        return text or None

    def _compute(self, prompt: str) -> dict:
        raw = self.llm.complete(
            [ChatMessage(role="user", content=prompt)],
            response_format=_SCHEMA,
        )
        parsed = json.loads(raw)
        return {
            "merge": bool(parsed["merge"]),
            "merged_text": str(parsed.get("merged_text") or ""),
        }
