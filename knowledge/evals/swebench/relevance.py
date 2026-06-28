"""U4: pre-treatment ``R_exist`` relevance oracle — does relevant knowledge exist?

WHY *pre-treatment* and *independent of the treatment arm*: the secondary analysis
stratifies the A/B effect on whether relevant knowledge was available in an
instance's org. If that stratifier were derived from whether the treatment agent
*actually queried* Praxis, it would be a post-treatment variable, and conditioning
on it would open a collider — biasing the very comparison it's meant to sharpen.
So we decide ``R_exist`` here, BEFORE any arm runs, by an oracle retrieval that does
not depend on the agent's behavior: we query the instance's org with the gold-changed
file paths + issue text and check whether at least one retrieved fact clears Praxis's
retrieval floor. The hit-rate across instances is itself a first-class deliverable
(it answers "is the direction worth scaling?"), independent of any effect size.

The retrieval floor reused here is the **existence floor** of
:class:`knowledge.graph_reader.grapher_reader_variants.retrieving_reader.RetrievingReader`
— its ``abs_floor`` (default ``0.30``): the coarse "is this even related" cutoff that
the reader applies first. ``R_exist=1`` iff the top hit's score ``>= ABS_FLOOR``. We
deliberately use only the absolute existence floor (not the relative-ratio shape knob),
because existence — "does *anything* relevant exist at all" — is exactly the
``abs_floor`` semantic; the relative cutoff is a per-query precision trim that only
makes sense once you've decided to inject.

Retrieval reuses U3's injectable client seam (``HttpClient`` / ``UrllibClient`` /
``get_context``) so this stays offline-testable: the same ``GET /context`` handler
already returns per-hit ``score`` values (see ``knowledge/serve/app.py``'s
``get_context``), so the oracle compares those scores against the floor directly
rather than reimplementing ranking. ``space_id_for(instance)`` pins the query to the
instance's own space.
"""

from __future__ import annotations

from dataclasses import dataclass

from knowledge.evals.swebench.ingest import HttpClient, space_id_for
from knowledge.evals.swebench.instances import Instance

# The existence floor reused from RetrievingReader.abs_floor (its default). A hit
# whose score clears this is "related enough to exist"; below it, nothing relevant.
# Imported as a named constant (not a magic number) so tests pin the comparison to
# the same value the reader's contract uses. Recompute on an embedding-model change,
# exactly as the reader's docstring instructs for abs_floor.
ABS_FLOOR: float = 0.30

# How many hits to pull from /context before applying the floor. Matches the reader's
# default search pool (RetrievingReader.top_k = 8); only the top hit decides R_exist.
DEFAULT_TOP_K: int = 8


@dataclass
class RelevanceResult:
    """The pre-treatment relevance verdict for one instance's org.

    * ``r_exist`` — True iff ≥1 retrieved fact clears :data:`ABS_FLOOR`.
    * ``top_score`` — the best hit's score (``None`` when retrieval returned nothing).
    * ``top_hit`` — the hit dict that triggered it (for case studies), or ``None``.
    * ``query`` — the oracle query used, recorded for auditability/reproducibility.
    """

    r_exist: bool
    top_score: float | None
    top_hit: dict | None
    query: str


def build_query(instance: Instance) -> str:
    """Oracle query = gold-changed file paths + the issue text.

    The gold-changed paths name *where* the fix lands; the issue text names *what*
    is broken. Together they're the retrieval probe for "is there knowledge about
    this region/problem in the org". Mirrors U3's ``leakage_guard`` query shape so
    the oracle and the leakage check probe the same surface.
    """
    return (" ".join(instance.gold_files) + " " + instance.problem_statement).strip()


def r_exist(
    instance: Instance,
    client: HttpClient,
    *,
    top_k: int = DEFAULT_TOP_K,
    abs_floor: float = ABS_FLOOR,
) -> RelevanceResult:
    """Decide whether relevant knowledge exists in ``instance``'s space.

    Queries the space (``space_id_for(instance)``) via the injected client, takes the
    top-scoring hit, and sets ``r_exist`` iff its score clears ``abs_floor``.
    Deterministic: a given space + instance yields the same verdict and score across
    calls (no randomness; the floor comparison is pure). Computed independent of the
    treatment arm — it never observes whether the agent queried Praxis.
    """
    space_id = space_id_for(instance)
    query = build_query(instance)
    ctx = client.get_context(space_id, query, top_k)
    hits = ctx.get("hits", []) or []

    top_hit: dict | None = None
    top_score: float | None = None
    for hit in hits:
        score = hit.get("score")
        if score is None:
            continue
        score = float(score)
        if top_score is None or score > top_score:
            top_score = score
            top_hit = hit

    if top_score is None or top_score < abs_floor:
        # Nothing cleared the existence floor — keep the top hit (if any) for case
        # studies, but R_exist is False and we report no triggering hit.
        return RelevanceResult(r_exist=False, top_score=top_score, top_hit=None, query=query)

    return RelevanceResult(r_exist=True, top_score=top_score, top_hit=top_hit, query=query)
