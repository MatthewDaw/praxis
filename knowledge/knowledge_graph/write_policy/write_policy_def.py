"""Shapes for the write-policy pipeline.

A ``WriteDecision`` flows through the ordered steps (each mutating it); the store
then enacts the final decision. ``StoreView`` is the read-only window a step gets
onto the existing facts plus similarity search.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from knowledge.knowledge_graph.knowledge_graph_def import SearchHit

Action = Literal["add", "noop", "update", "overwrite"]


@dataclass
class WriteDecision:
    """The mutable verdict for one candidate write, threaded through the steps."""

    text: str
    action: Action = "add"
    # Fact to act on for action == "update" (bump) or "overwrite" (replace in place).
    update_target_id: str | None = None
    # Extra contradicting facts to decay when action == "overwrite" (force-upsert).
    supersede_ids: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # e.g. ["contradiction:<id>"]
    dropped: bool = False  # a step suppressed this write entirely


class StoreView(Protocol):
    """The read-only window a write step has onto the store."""

    def most_similar(self, text: str, k: int = 5) -> list[SearchHit]:
        """Top-k existing facts most similar to ``text`` (best first)."""
