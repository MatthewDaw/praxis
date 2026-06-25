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

**Slot-guard (loss point B: over-merging distinct-but-overlapping facts).** Two
distinct facts can share heavy vocabulary — sibling table rows ("field X required:
yes" / "field Y required: yes"), or two prose rules about different subjects (a
participation-percentage definition vs a streak rule that mentions participation).
Cosine stays high, so the recall gate surfaces them and the precision judge may
wrongly fold them into one — silently losing one fact's identity. Ahead of the
stage-2 judge loop the guard consults the functional ``(subject, attribute)`` claims
``ClaimExtractor`` produced (``decision.claims``) against each candidate's functional
slots (from the slot-keyed recall ``decision.claim_candidates`` — the cosine
candidates carry no claims) and makes a three-way decision per candidate:

* **different slot** (no shared functional slot) -> these are distinct facts;
  **block** the merge (keep both active);
* **same slot, different value** -> a CONTRADICTION; **block** the merge and leave
  it for ``ClaimConflictDetector`` to flag (suppressing only the merge would trade
  silent-merge for silent-conflict);
* **same slot, same value** -> a genuine duplicate; **allow** the merge (this is
  what makes re-ingesting the same table — or the same rule — idempotent).

The ids the later ``Augmenter`` must also refuse — same-slot/different-value
**conflicts** (an additive merge would silence the contradiction) plus the tabular
fail-safe — are recorded on ``decision.no_merge_ids``. A merely **distinct-slot**
candidate is NOT put there: this step blocks its own verbatim same-lesson merge, but
the Augmenter's additive judge still arbitrates it, so a genuine additive note about
a related subject can fold in (the Mem0-style additive path is unchanged).

The guard engages for any write whose extractor produced a functional claim — prose
**and** tabular. A write with **no** functional claim (the natural case for additive
prose like "likes cheese pizza" + "loves chicken pizza", whose preference claims are
multi-valued) is left to the judges, so the additive-merge path is unchanged. The
one exception is the **tabular fail-safe**: a *tabular-flagged* row whose functional
claim is missing/empty (the extractor sometimes returns a null subject) is **demoted
to ``proposed``** for review rather than risk a wrong merge — fail toward keeping
rows distinct. (Prose without a functional claim is normal, not a failure, so it is
not demoted.)
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
    # The slot-guard reads decision.claim_candidates (functional-slot recall), so the
    # store must fill it — after ClaimExtractor, before this step. (ClaimConflictDetector
    # also consumes it; whichever runs first triggers the one shared slot recall.)
    consumes_claim_candidates = True

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

        # 1b. Slot-guard (runs AHEAD of the judge loop). Returns the set of candidate
        # ids THIS step's same-lesson judge must not merge; may itself decide an allowed
        # same-value merge or demote a missing-claim tabular write. No-op for a write with
        # no functional claim. It also records ``decision.no_merge_ids`` — the subset the
        # later Augmenter must refuse (same-slot conflicts + the tabular fail-safe), but
        # NOT distinct-slot facts, which the Augmenter's additive judge still arbitrates
        # (so the Mem0-style additive merge of two different-subject notes is unaffected).
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
        """Three-way slot decision; returns candidate ids the merge judges must NOT fold.

        Keyed on the full functional ``(subject, attribute)`` slot, not subject alone:
        a role x permission table is all ``subject = coach`` with the attribute varying,
        so subject-only would never fire and the judge would fold the rows into one.
        Engages for any write with a functional claim (prose and tabular).
        """
        incoming = {c.slot: c.value for c in decision.claims if c.functional}
        if not incoming:
            if TABULAR_FLAG in decision.flags:
                # Fail-safe: a tabular row with no functional claim (e.g. the extractor
                # returned a null subject) can't be slotted -> don't merge, demote to
                # ``proposed`` for review. Fail toward keeping rows distinct. The Augmenter
                # must not additively merge it away either, so record it on no_merge_ids.
                decision.state = "proposed"
                blocked = {hit.fact.id for hit in decision.candidates}
                decision.no_merge_ids = sorted(blocked)
                return blocked
            # Prose with no functional claim is normal (e.g. additive preferences) —
            # leave it to the MergeJudge/Augmenter; the guard does not engage.
            return set()

        # The cosine candidates carry no claims; the functional slots of existing facts
        # come from the slot-keyed recall (decision.claim_candidates), which both
        # substrates fill from the ``claims`` store. Build slot->value per candidate fact.
        existing_by_fact: dict[str, dict[tuple[str, str], str]] = {}
        for ch in decision.claim_candidates:
            existing_by_fact.setdefault(ch.fact.fact.id, {})[(ch.subject, ch.attribute)] = ch.value

        guarded: set[str] = set()  # blocks THIS step's same-lesson judge loop
        conflicts: set[str] = set()  # same-slot/different-value -> also block the Augmenter
        for hit in decision.candidates:
            existing = existing_by_fact.get(hit.fact.id, {})
            shared = incoming.keys() & existing.keys()
            if not shared:
                # Different slot -> distinct facts. Block this step's verbatim same-lesson
                # merge, but leave it to the Augmenter's additive judge (so a genuine
                # additive note about a related subject can still fold in).
                guarded.add(hit.fact.id)
                continue
            if all(_same(incoming[s], existing[s]) for s in shared):
                # Same slot(s), same value -> genuine duplicate. Allow the merge so a
                # re-ingested table (or restated rule) stays idempotent (N facts, not 2N).
                decision.action = "update"
                decision.update_target_id = hit.fact.id
                decision.no_merge_ids = sorted(conflicts)
                return guarded
            # Same slot, DIFFERENT value -> a contradiction. Block the merge here AND in
            # the Augmenter (an additive merge would silence the conflict), and leave the
            # pair for ClaimConflictDetector to flag.
            guarded.add(hit.fact.id)
            conflicts.add(hit.fact.id)
        decision.no_merge_ids = sorted(conflicts)
        return guarded


def _same(a: str, b: str) -> bool:
    """Two slot values are the same after the store's subject/attribute normalization."""
    return Claim.norm(a) == Claim.norm(b)
