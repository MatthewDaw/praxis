"""Mem0-style UPDATE/merge (augment): fold a related-but-additive note into an existing fact.

The gap the Deduper + ClaimConflictDetector path leaves open: two notes that are
neither the *same lesson* (Deduper skips them) nor a *contradiction*
(ClaimConflictDetector finds no incompatible functional-slot clash), but are
clearly about the same thing and *additive* — "likes cheese pizza" + "loves
chicken pizza". Mem0's reconciliation would UPDATE the existing memory into one
merged fact ("likes cheese and chicken pizza") rather than keeping two rows or
flagging a false contradiction.

The :class:`Augmenter` runs right after the Deduper (so exact/same-lesson dups are
already collapsed) and before the structural conflict detector (so a genuine
clash is still flagged, not silently merged). For each recalled candidate that is
related but not identical, it asks an :class:`AugmentJudge` whether the new note
should be *merged into* that existing fact and, if so, for the synthesized merged
text. On yes it sets ``action="augment"`` with ``update_target_id`` (the existing
fact to rewrite) and ``augment_text`` (the merged survivor text); the store's
``_augment`` rewrites that fact's text (re-embeds), bumps observation_count, and
keeps a single fact.

Determinism + graceful degradation mirror ``Deduper``/``MergeJudge``:
- a ``VerdictCassette`` replays committed verdicts offline (loud-miss on a stale one);
- with a live ``llm`` and no cassette, it computes directly (production path);
- with neither, ``merged_text`` returns ``None`` — the step is a no-op (the write
  falls through to add/conflict exactly as before).
"""

from __future__ import annotations

from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision
from knowledge.knowledge_graph.write_policy.write_step_variants.augment_judge import (
    AugmentJudge,
)


class Augmenter(WriteStep):
    """Fold a related-but-additive note into an existing fact (Mem0 UPDATE op)."""

    consumes_candidates = True

    def __init__(self, judge: AugmentJudge | None = None) -> None:
        self.judge = judge

    def apply(self, decision: WriteDecision) -> None:
        # Skip if a prior step already decided this write (exact/same-lesson dup),
        # or there's nothing to merge into, or no judge to decide.
        if decision.dropped or decision.action != "add":
            return
        if not decision.candidates or self.judge is None:
            return
        # The Deduper's slot-guard already ruled these candidates distinct (different
        # functional slot) or conflicting (same slot, different value); never fold an
        # additive merge into them, or we'd reintroduce the silent over-merge the guard
        # blocked one stage earlier.
        no_merge = set(decision.no_merge_ids)
        for hit in decision.candidates:
            if hit.fact.id in no_merge:
                continue
            # An exact dup would have been collapsed by the Deduper already; skip it
            # defensively so the judge never merges a fact into its own twin.
            if hit.fact.text.strip() == decision.text.strip():
                continue
            merged = self.judge.merged_text(decision.text, hit.fact.text)
            if merged and merged.strip():
                decision.action = "augment"
                decision.update_target_id = hit.fact.id
                decision.augment_text = merged.strip()
                return
