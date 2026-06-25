"""Semantic (paraphrase) contradiction fallback — Graphiti's two-stage detector.

The structural :class:`ClaimConflictDetector` only fires when two facts land on the
same *functional* (subject, attribute) slot with incompatible values. Paraphrase
contradictions that share NO normalized slot slip through it — e.g. "I love working
outdoors" vs "I can't stand being outside" canonicalize onto different slots, so no
structural clash is ever seen.

This second-pass detector closes that gap by borrowing Graphiti's two-stage shape:

  1. **Embedding-narrow** — reuse the single per-write cosine recall pass
     (``decision.candidates``); only near-neighbours are ever considered, so the LLM
     is asked about a handful of plausible pairs, never the whole graph.
  2. **LLM-judge** — for each candidate that the structural detector did NOT already
     flag and that does NOT share a functional slot with the incoming write, ask a
     narrow judge "does statement A logically contradict statement B?". On a clear
     yes, append a ``contradiction:<id>`` flag exactly like the structural path, so
     the existing ``_persist_contradictions`` machinery turns it into a ``fact_edges``
     contradiction edge.

Precision-first (mirrors ``ClaimValueJudge``): when the judge is unavailable, errors,
or is uncertain, the pair is **suppressed**, not flagged — a missed contradiction
beats a false one. This runs AFTER ``ClaimConflictDetector`` and never reconsiders a
pair the structural path already settled.
"""

from __future__ import annotations

import json

from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision
from knowledge.llm.llm_def import ChatMessage
from knowledge.llm.parent_llm import Llm
from knowledge.llm.verdict_cassette import VerdictCassette

_PROMPT = (
    "Two statements were recorded about the same person/topic.\n"
    "Does statement A logically CONTRADICT statement B — i.e. they cannot both be "
    "true at the same time?\n"
    "Answer contradicts true ONLY if they clearly, directly conflict (opposite "
    "claims about the same thing). If they are merely different, unrelated, or could "
    "both hold, answer false.\n"
    "STATEMENT A: {a}\nSTATEMENT B: {b}"
)

_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "contradiction_verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"contradicts": {"type": "boolean"}},
            "required": ["contradicts"],
            "additionalProperties": False,
        },
    },
}


class SemanticConflictJudge:
    """Decides whether two free-text statements logically contradict.

    Mirrors ``ClaimValueJudge``: structured ``{contradicts}`` over the LLM seam,
    cassette-replayed offline, ``None`` when there is no source (caller suppresses).
    """

    def __init__(
        self, llm: Llm | None = None, cassette: VerdictCassette | None = None
    ) -> None:
        self.llm = llm
        self.cassette = cassette

    def contradicts(self, a: str, b: str) -> bool | None:
        """True/False if a verdict is available; None to skip (no cassette, no llm)."""
        # Key the cassette on the exact rendered prompt, so editing the prompt (or the
        # inputs) is a clean miss, not a stale replay.
        prompt = _PROMPT.format(a=a, b=b)
        if self.cassette is not None:
            return self.cassette.verdict(prompt, lambda: self._compute(prompt))["contradicts"]
        if self.llm is not None:
            return self._compute(prompt)["contradicts"]
        return None

    def _compute(self, prompt: str) -> dict:
        raw = self.llm.complete(
            [ChatMessage(role="user", content=prompt)],
            response_format=_SCHEMA,
        )
        return {"contradicts": bool(json.loads(raw)["contradicts"])}


class SemanticConflictDetector(WriteStep):
    """Flag paraphrase contradictions among recalled neighbours with no shared slot.

    Second pass after ``ClaimConflictDetector``: reads the cosine-recalled
    ``decision.candidates`` and, for each that the structural detector did not flag
    and that shares no functional slot with the incoming write, asks the LLM judge.
    A clear yes appends a ``contradiction:<id>`` flag (precision-first otherwise).
    """

    # Reads the WIDER ``semantic_candidates`` recall (lower floor) — paraphrase
    # contradictions routinely fall just under the narrow dedup/conflict floor.
    consumes_semantic_candidates = True

    def __init__(self, judge: SemanticConflictJudge | None = None) -> None:
        self.judge = judge

    def apply(self, decision: WriteDecision) -> None:
        if decision.dropped or decision.action == "update":
            return
        if self.judge is None or not decision.semantic_candidates:
            return
        # Already-settled pairs: anything the structural detector flagged this write.
        flagged: set[str] = {
            f.split(":", 1)[1] for f in decision.flags if f.startswith("contradiction:")
        }
        # Functional slots the incoming write occupies — a candidate sharing one of
        # these is the STRUCTURAL detector's domain, so we never second-guess it here
        # (it ruled the values compatible, or already flagged them).
        incoming_slots = {c.slot for c in decision.claims if c.functional}
        for hit in decision.semantic_candidates:
            fact = hit.fact
            if fact.id in flagged:
                continue
            if incoming_slots and any(
                c.functional and c.slot in incoming_slots for c in fact.claims
            ):
                continue
            if self._contradicts(decision.text, fact.text):
                decision.flags.append(f"contradiction:{fact.id}")
                flagged.add(fact.id)

    def _contradicts(self, a: str, b: str) -> bool:
        # Precision-first: no judge or uncertain/error -> suppress (treat as no clash).
        try:
            verdict = self.judge.contradicts(a, b)
        except Exception:
            verdict = None
        return bool(verdict)
