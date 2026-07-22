"""Shapes for the write-policy pipeline.

A ``WriteDecision`` flows through the ordered steps (each mutating it); the store
then enacts the final decision. The store does **one** candidate-recall pass per
write (embed once, search once) and hands the result to the steps on
``WriteDecision.candidates`` — steps read that shared set rather than searching
themselves, so the incoming text is embedded exactly once (SC-007).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from knowledge.knowledge_graph.knowledge_graph_def import Claim, SearchHit

Action = Literal["add", "noop", "update", "overwrite", "augment"]

# The persisted marker (in ``Fact.meta``) that stamps a fact as a force/raw insert.
# A forced fact honors the documented ``raw=True`` contract: it is redacted and
# embedded but PERMANENTLY EXEMPT from the dedup/contradiction/merge pipeline — it
# lands ``active`` and is never auto-merged or auto-rejected, on its own write or on
# any later re-distill. The flag lives in ``meta`` so the exemption round-trips a
# save/reload: a re-submitted forced fact re-bypasses the pipeline, later writes
# never treat it as a merge/conflict target, and the auto-resolver refuses to reject
# it.
FORCED_META_KEY = "forced"

# The state a freshly-written fact is persisted with. Set by the caller of
# ``write`` (not by a policy step): "active" when the user directly approved the
# insertion, "proposed" when the system added it passively. ("rejected" is a
# retirement state the store assigns to superseded facts, never an entry state.)
SeedState = Literal["proposed", "active"]


@dataclass
class WriteDecision:
    """The mutable verdict for one candidate write, threaded through the steps.

    ``state`` is the lifecycle state the new fact lands in; it is decided by the
    caller (direct approval -> "active", passive add -> "proposed") and the steps
    leave it alone — they only decide add/dedup/conflict, not endorsement.

    ``embedding`` and ``candidates`` are filled once by the store before the steps
    run: the incoming text's vector and the single recall pass (existing facts
    above the shared ``recall_floor``, best first). Persistence reuses
    ``embedding`` so the write embeds the text exactly once.
    """

    text: str
    state: SeedState = "proposed"
    action: Action = "add"
    # A force/raw insert (``praxis_add_insight(raw=True)`` or ``write(forced=True)``).
    # A forced write honors the documented raw contract literally: it is redacted and
    # embedded but PERMANENTLY EXEMPT from the dedup/contradiction/merge pipeline — it
    # lands ``active`` and is never auto-merged or auto-rejected, on its own write or on
    # any later re-distill. The store persists ``meta["forced"]=True`` so the exemption
    # round-trips a save/reload cycle (recall excludes forced facts as merge/conflict
    # targets; the auto-resolver refuses to reject them).
    forced: bool = False
    # Fact to act on for action == "update" (bump), "overwrite" (replace in place),
    # or "augment" (rewrite the target's text with a Mem0-style merged survivor).
    update_target_id: str | None = None
    # Synthesized merged survivor text for action == "augment" (set by Augmenter):
    # the existing fact identified by update_target_id is rewritten to this.
    augment_text: str | None = None
    # The id of the fact the store actually appended for action == "add"/"overwrite"
    # (a fresh row). Filled by the store after persistence so callers can map a write
    # back to its stored fact without diffing ``facts``; ``None`` until then.
    added_fact_id: str | None = None
    # Extra contradicting facts to decay when action == "overwrite" (force-upsert).
    supersede_ids: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # e.g. ["contradiction:<id>"]
    dropped: bool = False  # a step suppressed this write entirely
    # This write carries a non-empty ``derived_from`` (it explicitly declares a NEW
    # fact built on a source). A derivation must stay its own distinct node carrying
    # the derivation edge — never folded back into its source — so the merge steps
    # (Deduper's same-lesson merge, Augmenter's additive merge) refuse to merge it.
    # Keyed on derived_from presence, not category. Set by ``write()`` before steps run.
    derived: bool = False
    # Candidate fact ids the slot-guard ruled distinct (different functional slot) or
    # conflicting (same slot, different value) from this write — so a later merge step
    # (Augmenter) must NOT fold this write into them. Filled by the Deduper's slot-guard.
    no_merge_ids: list[str] = field(default_factory=list)
    # Declared derivation provenance (gap H5): the source ids this write was
    # explicitly derived from. A write carrying derived_from is a *new* fact built
    # on its sources (e.g. a learning about how a requirement was implemented), not
    # a duplicate — so a merge step (Augmenter) must NEVER fold it into another fact,
    # or the distinct node + its derived_from edge are lost. Set by the store from
    # the write() call; empty for an ordinary write.
    derived_from: list[str] = field(default_factory=list)
    # The category the caller stamped on this write (e.g. "requirement", "learning").
    # Carried onto the decision so a merge step can refuse to fold across distinct
    # category values (a "learning" must not be merged into a "requirement").
    category: str | None = None
    # Shared per-write recall (filled by the store before the steps run).
    embedding: list[float] | None = None
    candidates: list[SearchHit] = field(default_factory=list)
    # A WIDER cosine recall (lower floor, larger k) the store fills for the
    # semantic contradiction fallback only. Paraphrase contradictions often sit
    # just below the dedup/conflict ``recall_floor`` (cosine ~0.39-0.44), so the
    # narrow ``candidates`` set misses them; this wider set is reserved for the
    # semantic LLM judge (whose own gate supplies precision) without widening — and
    # thus changing the cost/behavior of — the dedup/augment/structural paths.
    semantic_candidates: list[SearchHit] = field(default_factory=list)
    # Tier-B (gated): controlled-vocabulary aspect tags assigned to the incoming
    # note by AspectTagger, persisted to Fact.tags; and the bounded same-tag recall
    # (existing facts sharing a tag) the store adds for the conflict path only.
    tags: list[str] = field(default_factory=list)
    tag_candidates: list[SearchHit] = field(default_factory=list)
    # Structural contradiction path: atomic (subject, attribute, value) claims
    # extracted from the incoming text by ClaimExtractor (persisted alongside the
    # fact), and the claim-keyed recall the store fills for ClaimConflictDetector —
    # existing facts sharing a functional (subject, attribute) slot with this write.
    claims: list[Claim] = field(default_factory=list)
    claim_candidates: list["ClaimHit"] = field(default_factory=list)


@dataclass
class ClaimHit:
    """An existing fact that shares a functional slot with the incoming write.

    Carries the matched slot plus the existing fact's value(s) on it, so the
    conflict detector can compare values without re-deriving the slot.
    """

    fact: SearchHit
    subject: str  # normalized slot subject
    attribute: str  # normalized slot attribute
    value: str  # the existing fact's raw value on this slot


def demote_active_contradiction(decision: WriteDecision) -> None:
    """Enforce FR-005 in place: a forced-``active`` write that the policy flagged
    as contradicting an already-``active`` fact is dropped to ``proposed``.

    Two contradicting facts are never both active; the newcomer becomes a pending
    contradiction (the reviewer resolves it) instead of a second active side. The
    contradiction edge is still recorded by the store, so the pair stays linked.
    No-op unless the write is active and a ``contradiction:<id>`` flag targets a
    recall candidate that is itself active.
    """
    if decision.state != "active":
        return
    contradicted = {
        f.split(":", 1)[1] for f in decision.flags if f.startswith("contradiction:")
    }
    if not contradicted:
        return
    by_id = {
        hit.fact.id: hit.fact
        for hit in (*decision.candidates, *decision.tag_candidates)
    }
    if any(cid in by_id and by_id[cid].state == "active" for cid in contradicted):
        decision.state = "proposed"
