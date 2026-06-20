"""Force-supersede a candidate's contradictions instead of just flagging them.

The approved-insight path (a human confirmed the wording in chat) wants the new
note to *win*: where :class:`ConflictFlagger` records a ``contradiction:<id>``
flag for later human resolution, this step turns the same LLM-confirmed
contradiction into an ``overwrite`` decision — the new text replaces the nearest
conflicting fact in place, and any other contradictions are marked to decay. So
no contradictory pair lingers; the newest approved truth is the single survivor.

Like :class:`ConflictFlagger` it's best-effort: with no LLM, or if detection
fails, it leaves the decision untouched (a plain ``add``).
"""

from __future__ import annotations

from knowledge.llm.llm_def import ChatMessage
from knowledge.llm.parent_llm import Llm
from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_policy_def import StoreView, WriteDecision

_PROMPT = (
    "Does the NEW note contradict the EXISTING note? "
    "Answer only 'yes' or 'no'.\nEXISTING: {existing}\nNEW: {new}"
)


class ConflictOverwriter(WriteStep):
    """On a confirmed contradiction, overwrite the conflicting fact in place."""

    def __init__(self, llm: Llm | None = None, similarity_floor: float = 0.6) -> None:
        self.llm = llm
        self.similarity_floor = similarity_floor

    def apply(self, decision: WriteDecision, store: StoreView) -> None:
        # A dedup match already won; don't fight it. No LLM => can't detect.
        if self.llm is None or decision.dropped or decision.action != "add":
            return
        conflicts: list[str] = []
        for hit in store.most_similar(decision.text, k=3):
            if hit.score < self.similarity_floor:
                break
            prompt = _PROMPT.format(existing=hit.fact.text, new=decision.text)
            try:
                answer = self.llm.complete([ChatMessage(role="user", content=prompt)])
            except Exception:
                # Detection unavailable — leave it a plain add (best-effort).
                return
            if answer.strip().lower().startswith("yes"):
                conflicts.append(hit.fact.id)
        if not conflicts:
            return
        # Overwrite the nearest conflict in place; decay the rest.
        decision.action = "overwrite"
        decision.update_target_id = conflicts[0]
        decision.supersede_ids = conflicts[1:]
