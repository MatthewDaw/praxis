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

from knowledge.knowledge_graph.knowledge_graph_def import SearchHit

Action = Literal["add", "noop", "update", "overwrite"]

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
    # Fact to act on for action == "update" (bump) or "overwrite" (replace in place).
    update_target_id: str | None = None
    # Extra contradicting facts to decay when action == "overwrite" (force-upsert).
    supersede_ids: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # e.g. ["contradiction:<id>"]
    dropped: bool = False  # a step suppressed this write entirely
    # Shared per-write recall (filled by the store before the steps run).
    embedding: list[float] | None = None
    candidates: list[SearchHit] = field(default_factory=list)
    # Tier-B (gated): controlled-vocabulary aspect tags assigned to the incoming
    # note by AspectTagger, persisted to Fact.tags; and the bounded same-tag recall
    # (existing facts sharing a tag) the store adds for the conflict path only.
    tags: list[str] = field(default_factory=list)
    tag_candidates: list[SearchHit] = field(default_factory=list)
