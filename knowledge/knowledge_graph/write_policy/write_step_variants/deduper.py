"""Deduplicate a candidate against existing facts (exact + semantic).

Two stages, recall then precision:

1. **Exact-match short-circuit** — byte-identical text collapses to an ``update``
   (bump the existing fact). Works with any embedder, no judge needed.
2. **Semantic merge** — a :class:`MergeJudge` (precision) decides whether each
   recalled candidate records the same lesson. No cosine threshold decides the
   merge — the judge does, and it keeps the existing note verbatim.

Recall is the store's job: it runs one candidate pass per write (a *loose*,
high-recall ``recall_floor``) and hands the result over on
``decision.candidates``. The Deduper reads that shared set — its only job here is
"don't miss a true dup". With the offline ``FakeEmbedder`` paraphrases score far
below the floor, so offline behavior is exact-dedup only; a real embedder + judge
enables paraphrase merge. With no judge wired, only stage 1 runs (graceful).

**Slot-guard (tabular rows).** Sibling rows of a table differ only in a key
("field X required: yes" / "field Y required: yes"); even identity-folded text
shares ~80% of tokens, so cosine stays high and the judge gets consulted and may
wrongly fold two distinct requirements into one. Ahead of the stage-2 judge loop
the guard consults the functional ``(subject, attribute)`` claims ``ClaimExtractor``
already produced (``decision.claims``) against each candidate fact's claims and
makes a three-way decision per candidate:

* **different slot** (no shared functional slot) -> these are distinct facts;
  **block** the merge (keep both active);
* **same slot, different value** -> a CONTRADICTION; **block** the merge and leave
  it for ``ClaimConflictDetector`` to flag (suppressing only the merge would trade
  silent-merge for silent-conflict);
* **same slot, same value** -> a genuine duplicate; **allow** the merge (this is
  what makes re-ingesting the same table idempotent).

Fail-safe: a *tabular-flagged* write whose functional claim is missing/empty (the
extractor LLM sometimes returns a null subject) cannot be slotted, so it is
**demoted to ``proposed``** for review rather than risk a wrong merge — fail toward
keeping rows distinct. The guard only engages for tabular-flagged writes, so prose
dedup is unchanged.
"""

from __future__ import annotations

from knowledge.knowledge_graph.knowledge_graph_def import Claim
from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision
from knowledge.knowledge_graph.write_policy.write_step_variants.merge_judge import MergeJudge

# Flag set by the ingestion side on a write distilled from tabular/templated input.
# Only these writes engage the slot-guard (prose dedup behavior is unchanged).
TABULAR_FLAG = "tabular"


class Deduper(WriteStep):
    consumes_candidates = True

    def __init__(self, judge: MergeJudge | None = None) -> None:
        self.judge = judge

    def apply(self, decision: WriteDecision) -> None:
        if decision.dropped:
            return
        candidates = decision.candidates  # shared recall pass, best-first, above the floor
        if not candidates:
            return

        # 1. Exact-match short-circuit (any embedder, no judge).
        for hit in candidates:
            if hit.fact.text.strip() == decision.text.strip():
                decision.action = "update"
                decision.update_target_id = hit.fact.id
                return

        # 1b. Slot-guard for tabular rows (runs AHEAD of the judge loop). Returns the
        # set of candidate ids the judge must not merge; may itself decide an allowed
        # same-value merge or demote a missing-claim write. No-op for prose writes.
        guarded = self._slot_guard(decision)
        if decision.action == "update" or decision.dropped:
            return  # guard already settled it (genuine duplicate -> merge)

        # 2. Semantic merge: judge each candidate. Skipped entirely without a judge.
        if self.judge is None:
            return
        for hit in candidates:
            if hit.fact.id in guarded:
                continue  # slot-guard ruled this a distinct fact / contradiction
            if self.judge.same_lesson(decision.text, hit.fact.text):
                decision.action = "update"  # merge into the existing verbatim survivor
                decision.update_target_id = hit.fact.id
                return

    def _slot_guard(self, decision: WriteDecision) -> set[str]:
        """Three-way slot decision for tabular rows; returns candidate ids to NOT merge.

        Keyed on the full functional ``(subject, attribute)`` slot, not subject alone:
        a role x permission table is all ``subject = coach`` with the attribute varying,
        so subject-only would never fire and the judge would fold the rows into one.
        """
        if TABULAR_FLAG not in decision.flags:
            return set()  # only tabular-flagged writes engage the guard

        incoming = {c.slot: c.value for c in decision.claims if c.functional}
        if not incoming:
            # Fail-safe: a tabular row with no functional claim (e.g. the extractor
            # returned a null subject) can't be slotted -> don't merge, demote to
            # ``proposed`` for review. Fail toward keeping rows distinct.
            decision.state = "proposed"
            return {hit.fact.id for hit in decision.candidates}

        guarded: set[str] = set()
        for hit in decision.candidates:
            existing = {c.slot: c.value for c in hit.fact.claims if c.functional}
            shared = incoming.keys() & existing.keys()
            if not shared:
                guarded.add(hit.fact.id)  # different slot -> distinct facts, block merge
                continue
            if all(_same(incoming[s], existing[s]) for s in shared):
                # Same slot(s), same value -> genuine duplicate. Allow the merge so a
                # re-ingested table stays idempotent (N facts, not 2N).
                decision.action = "update"
                decision.update_target_id = hit.fact.id
                return guarded
            # Same slot, DIFFERENT value -> a contradiction. Block the merge but leave
            # the pair for ClaimConflictDetector to flag (don't silence the conflict).
            guarded.add(hit.fact.id)
        return guarded


def _same(a: str, b: str) -> bool:
    """Two slot values are the same after the store's subject/attribute normalization."""
    return Claim.norm(a) == Claim.norm(b)
