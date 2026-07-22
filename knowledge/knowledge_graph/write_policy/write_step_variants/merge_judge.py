"""LLM merge-judge: do two notes record the SAME lesson, just phrased differently?

The precision arbiter for semantic dedup — no cosine threshold decides the merge.
Given the incoming text and an existing candidate fact, it asks an injected ``Llm`` a
tight yes/no question. The EXISTING fact is the verbatim survivor (the judge selects,
it never rewrites), so the answer the cassette stores is just ``same_lesson``; the
surviving id is the candidate, resolved by the caller at write time.

Determinism + graceful degradation, mirroring ``ConflictFlagger``:
- a ``VerdictCassette`` replays committed verdicts offline (loud-miss on a stale one);
- with a live ``llm`` and no cassette, it computes directly (production path);
- with neither, ``same_lesson`` returns ``None`` — the caller skips the semantic merge
  (exact-dedup still applies).
"""

from __future__ import annotations

from knowledge.knowledge_graph.write_policy.cassette_judge import CassetteJudge

_PROMPT = (
    "Do these two notes record the SAME lesson or rule, just phrased differently? "
    "Set same_lesson true if they do, false otherwise.\nEXISTING: {existing}\nNEW: {new}"
)

# Structured output: a JSON object (a bare boolean is not a valid json_schema root).
_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "merge_verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"same_lesson": {"type": "boolean"}},
            "required": ["same_lesson"],
            "additionalProperties": False,
        },
    },
}


class MergeJudge(CassetteJudge):
    """Decides whether an incoming note duplicates an existing one (same lesson)."""

    def same_lesson(self, incoming: str, existing: str) -> bool | None:
        """True/False if a verdict is available; None to skip (no cassette, no llm)."""
        prompt = _PROMPT.format(existing=existing, new=incoming)
        verdict = self._verdict(prompt, lambda: self._compute(prompt))
        return verdict["same_lesson"] if verdict is not None else None

    def _compute(self, prompt: str) -> dict:
        return {"same_lesson": bool(self._complete_json(prompt, _SCHEMA)["same_lesson"])}
