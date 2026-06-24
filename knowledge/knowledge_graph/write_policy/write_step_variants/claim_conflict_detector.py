"""Structural contradiction detection over extracted claims.

Replaces the cosine-recall + bare-boolean ``ConflictFlagger``/``ConflictJudge``
path. Reads the claim-keyed slot recall (``decision.claim_candidates``) and the
incoming write's own claims (``decision.claims``): when both hold a *functional*
claim on the same (subject, attribute) slot with **incompatible** values, it
records a ``contradiction:<id>`` flag, which the store materializes as a
``fact_edges`` row exactly as before.

Two-stage value comparison keeps it precise and cheap:
  1. equal normalized values -> not a conflict;
  2. both clearly numeric and different -> a conflict (no LLM call);
  3. otherwise -> the narrow ``ClaimValueJudge`` decides synonym vs genuine clash.

Precision-first (FR R3): when the gray-zone judge is unavailable or uncertain, the
pair is **suppressed**, not flagged — a missed conflict beats a false one.
"""

from __future__ import annotations

import json
import re

from knowledge.knowledge_graph.knowledge_graph_def import Claim
from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision
from knowledge.llm.llm_def import ChatMessage
from knowledge.llm.parent_llm import Llm
from knowledge.llm.verdict_cassette import VerdictCassette

_PROMPT = (
    "Two facts assert a value for the same property ({subject} -> {attribute}). "
    "Are these two values INCOMPATIBLE (cannot both be true), or are they the same "
    "value / synonyms / compatible refinements?\n"
    "Set incompatible true only if they genuinely conflict.\n"
    "VALUE A: {a}\nVALUE B: {b}"
)

_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "value_verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"incompatible": {"type": "boolean"}},
            "required": ["incompatible"],
            "additionalProperties": False,
        },
    },
}

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _norm(v: str) -> str:
    return " ".join(v.lower().split())


def _numeric_tokens(v: str) -> list[str]:
    return _NUM.findall(v)


class ClaimValueJudge:
    """Decides whether two values for the same functional slot are incompatible.

    Mirrors ``ConflictJudge``: structured ``{incompatible}`` over the LLM seam,
    cassette-replayed offline, ``None`` when no source (caller suppresses).
    """

    def __init__(
        self, llm: Llm | None = None, cassette: VerdictCassette | None = None
    ) -> None:
        self.llm = llm
        self.cassette = cassette

    def incompatible(self, subject: str, attribute: str, a: str, b: str) -> bool | None:
        """True/False if a verdict is available; None to skip (no cassette, no llm)."""
        # Key the cassette on the exact rendered prompt, so editing the prompt (or the
        # inputs) is a clean miss, not a stale replay.
        prompt = _PROMPT.format(subject=subject, attribute=attribute, a=a, b=b)
        if self.cassette is not None:
            return self.cassette.verdict(prompt, lambda: self._compute(prompt))["incompatible"]
        if self.llm is not None:
            return self._compute(prompt)["incompatible"]
        return None

    def _compute(self, prompt: str) -> dict:
        raw = self.llm.complete(
            [ChatMessage(role="user", content=prompt)],
            response_format=_SCHEMA,
        )
        return {"incompatible": bool(json.loads(raw)["incompatible"])}


class ClaimConflictDetector(WriteStep):
    """Flag contradictions from same-slot functional claims with incompatible values."""

    consumes_claim_candidates = True

    def __init__(self, judge: ClaimValueJudge | None = None) -> None:
        self.judge = judge

    def apply(self, decision: WriteDecision) -> None:
        if decision.dropped or decision.action == "update":
            return
        # Incoming functional claims, indexed by slot -> value (last wins).
        incoming: dict[tuple[str, str], str] = {
            c.slot: c.value for c in decision.claims if c.functional
        }
        if not incoming or not decision.claim_candidates:
            return
        flagged: set[str] = set()
        for hit in decision.claim_candidates:
            slot = (Claim.norm(hit.subject), Claim.norm(hit.attribute))
            new_val = incoming.get(slot)
            if new_val is None:
                continue
            fact_id = hit.fact.fact.id
            if fact_id in flagged:
                continue
            if self._incompatible(slot, new_val, hit.value):
                decision.flags.append(f"contradiction:{fact_id}")
                flagged.add(fact_id)

    def _incompatible(self, slot: tuple[str, str], a: str, b: str) -> bool:
        if _norm(a) == _norm(b):
            return False  # same value -> agreement
        if slot[1] == "stance":
            # A stance value is constrained to one of the axis's two named poles, so
            # two *different* poles are opposing by construction — a deterministic
            # clash needing no value judge (the axis subject already encodes the poles).
            return True
        an, bn = _numeric_tokens(a), _numeric_tokens(b)
        if an and bn:
            # Both carry numbers: a clear, deterministic clash (e.g. 1799 vs 1800)
            # needs no LLM. Equal number sets already returned above via _norm only
            # when whole strings matched, so compare the numeric tokens directly.
            return an != bn
        # Fuzzy (free-text) values: the narrow gray-zone judge decides. Precision
        # -first — no judge or uncertain -> suppress (treat as not incompatible).
        verdict = None
        if self.judge is not None:
            try:
                verdict = self.judge.incompatible(slot[0], slot[1], a, b)
            except Exception:
                verdict = None
        return bool(verdict)
