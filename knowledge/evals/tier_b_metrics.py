"""Tier-B gate metrics (FR-022): measure the implicit-contradiction experiment.

For each ``implicit_conflict_*`` case, seed its two disjoint-vocab contradictory
notes through the real write policy (AspectTagger -> Deduper -> ConflictFlagger,
replayed offline from the committed aspect/conflict cassettes) and report the two
gate metrics the owner reviews:

- **tag co-assignment recall** — fraction of pairs where the AspectTagger gave both
  notes at least one shared aspect tag (the recall key that must fire for the pair
  to be surfaced at all);
- **end-to-end flag rate** — fraction of pairs the system actually flags as a
  contradiction (tag recall surfaced the candidate AND the ConflictJudge agreed);
- **cosine baseline** — each pair's real cosine similarity and whether the SAME
  conflict path *without* the AspectTagger would flag it. "Rescued by tags" = the
  pair the tag key catches that cosine recall alone misses. This is the honest
  load-bearing check: if cosine already flags a pair, the tag added nothing there.

No fixed threshold is pinned: the numbers are surfaced for an explicit human
keep/kill decision (FR-022). Run offline (replay-only):

    OPENROUTER_API_KEY= PHOENIX_COLLECTOR_ENDPOINT= uv run python -m knowledge.evals.tier_b_metrics
"""

from __future__ import annotations

from knowledge.evals.run import (
    _build_trio_for,
    _conflict_judge_for,
    _eval_embedder,
    _merge_judge_for,
    load_cases,
    load_env,
)


def _flagged_cosine_only(case) -> bool:
    """Re-seed the pair through the conflict path WITHOUT the AspectTagger."""
    from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import VectorGraph
    from knowledge.knowledge_graph.write_policy.write_step_variants import (
        ConflictFlagger,
        Deduper,
        Redactor,
    )

    policy = [Redactor(), Deduper(judge=_merge_judge_for(case))]
    conflict_judge = _conflict_judge_for(case)
    if conflict_judge is not None:
        policy.append(ConflictFlagger(judge=conflict_judge))
    graph = VectorGraph(embedder=_eval_embedder(case), policy=policy)
    for text in case.seeded_insight.direct_to_graph:
        graph.write(text)
    return len(graph.contradictions()) > 0


def measure() -> int:
    from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import _cosine

    cases = [c for c in load_cases() if c.id.startswith("implicit_conflict_")]
    if not cases:
        print("no implicit_conflict_* cases found")
        return 1

    co_assigned = flagged = flagged_cosine = rescued = 0
    print(f"{'case':<42} {'cos':>5}  {'shared-tag':<11} {'flag(tag)':<10} {'flag(cos)':<10}")
    for case in sorted(cases, key=lambda c: c.id):
        graph, _, _ = _build_trio_for(case)
        for text in case.seeded_insight.direct_to_graph:
            graph.write(text)
        facts = graph.facts
        tagsets = [set(f.tags) for f in facts]
        shared = bool(set.intersection(*tagsets)) if len(tagsets) > 1 else False
        is_flagged = len(graph.contradictions()) > 0
        cos = (
            _cosine(facts[0].embedding, facts[1].embedding)
            if len(facts) > 1 and facts[0].embedding and facts[1].embedding
            else float("nan")
        )
        cos_flag = _flagged_cosine_only(case)
        co_assigned += shared
        flagged += is_flagged
        flagged_cosine += cos_flag
        rescued += is_flagged and not cos_flag
        print(
            f"{case.id:<42} {cos:>5.2f}  {('yes' if shared else 'no'):<11} "
            f"{('yes' if is_flagged else 'no'):<10} {('yes' if cos_flag else 'no'):<10}"
        )

    n = len(cases)
    print(f"\nimplicit-contradiction pairs: {n}")
    print(f"tag co-assignment recall:  {co_assigned}/{n} = {co_assigned / n:.2f}")
    print(f"end-to-end flag rate:      {flagged}/{n} = {flagged / n:.2f}")
    print(f"cosine-only flag rate:     {flagged_cosine}/{n} = {flagged_cosine / n:.2f}  (baseline)")
    print(f"rescued by tags (tag-only):{rescued}/{n} = {rescued / n:.2f}  (the load-bearing signal)")
    return 0


if __name__ == "__main__":
    load_env()
    raise SystemExit(measure())
