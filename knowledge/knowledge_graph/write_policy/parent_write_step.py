"""Abstract write-policy step.

Each step does one thing to a :class:`WriteDecision` (redact, dedup, flag) and is
composed in order by the store. Steps are independently testable and swappable.

Steps no longer search the store themselves: the store does one candidate-recall
pass per write and hands it over on ``WriteDecision.candidates``. A step that
reads those candidates sets ``consumes_candidates = True`` so the store knows to
populate them (embedding the incoming text once) before the step runs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision


class WriteStep(ABC):
    """One stage of the write-time policy pipeline."""

    # True if ``apply`` reads ``decision.candidates`` (dedup/conflict steps). The
    # store fills the shared cosine recall pass before the first such step runs.
    consumes_candidates: bool = False

    # True if ``apply`` reads ``decision.claim_candidates`` (the structural
    # contradiction detector). The store fills the claim-keyed slot recall before
    # the first such step runs — separately from the cosine pass, because it must
    # happen AFTER ClaimExtractor has populated ``decision.claims``.
    consumes_claim_candidates: bool = False

    # True if ``apply`` reads ``decision.semantic_candidates`` (the semantic
    # contradiction fallback). The store fills a WIDER, lower-floor cosine recall
    # before the first such step runs, reusing the already-computed embedding.
    consumes_semantic_candidates: bool = False

    @abstractmethod
    def apply(self, decision: WriteDecision) -> None:
        """Mutate ``decision`` in place. Single responsibility per step."""
