"""Shapes for the knowledge store (stored facts + search results)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# A fact's lifecycle state in the graph:
#   * "proposed" -- passively added by the system (e.g. distilled by the
#     ingestor); staged, not yet endorsed.
#   * "active"   -- the user directly approved this write; it is live knowledge.
#   * "decayed"  -- superseded/retired (e.g. lost a contradiction to a newer
#     approved fact); kept for provenance but no longer authoritative.
FactState = Literal["proposed", "active", "decayed"]


class Claim(BaseModel):
    """An atomic (subject, attribute, value) assertion extracted from a fact.

    The unit the structural contradiction detector reasons over: two facts whose
    claims share a ``subject`` and a *functional* ``attribute`` but hold
    incompatible ``value``s contradict each other. ``subject`` and ``attribute``
    are stored normalized (lowercased, whitespace-collapsed) so slot matching is
    robust to surface variation; ``value`` keeps its raw form.
    """

    subject: str
    attribute: str
    value: str
    # True when the attribute is single-valued for this subject (an event's year,
    # a birth year) -> a differing value is a contradiction. False for naturally
    # multi-valued attributes (discoveries, list members) -> values coexist.
    functional: bool = False

    @staticmethod
    def norm(s: str) -> str:
        """Normalize a subject/attribute for slot matching."""
        return " ".join(s.lower().split())

    @property
    def slot(self) -> tuple[str, str]:
        """The normalized (subject, attribute) key this claim occupies."""
        return (self.norm(self.subject), self.norm(self.attribute))


class Fact(BaseModel):
    """A stored unit of knowledge with its metadata.

    The persisted form of an :class:`~knowledge.injestion.injestion_def.Insight`
    plus storage bookkeeping. ``embedding`` is optional so a fact can exist
    before/without a vector (e.g. exact-dedup paths).
    """

    id: str
    text: str
    source: str | None = None
    confidence: float = 1.0
    scope: str | None = None
    category: str | None = None
    observation_count: int = 1
    state: FactState = "proposed"  # set by the write decision; see FactState
    # Topic cluster assigned by the clustering pass (embed -> reduce -> HDBSCAN).
    # None => unclustered (HDBSCAN noise). Ids are not stable across re-runs.
    cluster_id: int | None = None
    cluster_label: str | None = None
    embedding: list[float] | None = None
    flags: list[str] = Field(default_factory=list)  # e.g. ["contradiction:<id>"]
    # Wall-clock the row was first persisted (ISO 8601), set by the store on read.
    created_at: str | None = None
    # Free-form per-fact metadata (e.g. dashboard auditTrail), persisted in the
    # ``facts.meta`` jsonb column.
    meta: dict[str, Any] = Field(default_factory=dict)
    # Controlled-vocabulary aspect labels assigned at write time (Tier-B gated
    # experiment): a second, non-similarity recall key for the conflict path.
    tags: list[str] = Field(default_factory=list)
    # Atomic (subject, attribute, value) claims extracted from ``text`` at write
    # time; the structural contradiction detector reasons over these. Persisted to
    # the ``claims`` table (Postgres) or held on the fact (in-memory).
    claims: list["Claim"] = Field(default_factory=list)


class SearchHit(BaseModel):
    """A fact returned by ``SearchableGraph.search`` with its relevance score."""

    fact: Fact
    score: float = 0.0


class Contradiction(BaseModel):
    """A detected contradiction between a newly-written fact and an existing one.

    Surfaced for human review (the elevation surface) — the dashboard's
    Contradictions tab consumes these pairs.
    """

    flagged: Fact  # the newer fact whose write tripped the conflict check
    conflicting: Fact  # the existing fact it appears to contradict
