"""Flag a candidate that contradicts an existing, similar fact.

Reads the store's shared recall pass (``decision.candidates``) and asks an
injected :class:`ConflictJudge` (structured ``{contradicts}`` over an ``Llm``,
replayed from a verdict cassette offline) whether the new text contradicts each
candidate; if so, records a ``contradiction:<id>`` flag against the candidate's
runtime id (the store keeps the fact but marks it for human/automated
resolution). With no judge the step is inert — conflict handling is opt-in for
this baseline. Skipped when the write already merged (``action == "update"``):
merge runs before conflict, and a merged dup needs no conflict check.
"""

from __future__ import annotations

from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision
from knowledge.knowledge_graph.write_policy.write_step_variants.conflict_judge import (
    ConflictJudge,
)


class ConflictFlagger(WriteStep):
    consumes_candidates = True

    def __init__(self, judge: ConflictJudge | None = None) -> None:
        self.judge = judge

    def apply(self, decision: WriteDecision) -> None:
        if self.judge is None or decision.dropped or decision.action == "update":
            return
        # Conflict-path recall = cosine candidates ∪ same-tag candidates (Tier-B,
        # gated; tag_candidates is empty unless an AspectTagger ran). Dedup by id so
        # a fact recalled by both keys is judged once.
        seen: set[str] = set()
        for hit in [*decision.candidates, *decision.tag_candidates]:
            if hit.fact.id in seen:
                continue
            seen.add(hit.fact.id)
            try:
                verdict = self.judge.contradicts(decision.text, hit.fact.text)
            except Exception:
                # Detection unavailable (e.g. no API key / network) — skip the
                # check rather than failing the write. Detection is best-effort.
                return
            if verdict:
                decision.flags.append(f"contradiction:{hit.fact.id}")
