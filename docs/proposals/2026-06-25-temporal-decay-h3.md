# Proposal: Temporal Decay / Staleness (gap H3)

**Status**: Draft · **Raised**: 2026-06-25 · **Owner**: TBD
**Source**: agent-factory gap **H3** (`agent_factory/docs/praxis-gaps.md`).

---

## Problem

Praxis invalidates a fact only via contradiction or explicit edit — there is no
*time-based* aging. A learning captured a year ago and never re-confirmed competes in
retrieval on equal footing with today's truth. The memory-safety best practice is to
**decay** stale entries so the poisoning/stale-recall surface shrinks ("this hasn't been
confirmed in a long time → weight it less"). H1 made retrieval *outcome*-aware; H3 makes
it *recency*-aware. The two multiply.

## What already exists

H1's ranking is `score = similarity * utility` in `_search_vec`
([postgres_vector_graph.py](../../knowledge/knowledge_graph/knowledge_graph_variants/postgres_vector_graph.py)).
Facts already carry `created_at` (insert time) and bitemporal `valid_at`/`invalid_at`. So
H3 is one more multiplicative factor on the existing score — no schema change.

## What I'm building

- **Recency factor** folded into the cosine ranking:
  `score *= exp(-ln2 * age / half_life)` where `age = now() - created_at`. Half-life
  `_RECENCY_HALF_LIFE_DAYS = 90` (a fact ~90d old → ~0.5 weight). Neutral (~1.0) for
  freshly-written facts, so anything recent is unaffected — no regression for existing
  retrieval. `created_at IS NULL` ⇒ factor 1.0.
- **Scope — retrieval only.** `search(..., decay=True)` (default on). Explicitly **not**:
  - **write-time recall** (`_recall`/`_recall_semantic` call `_search_vec` with
    `apply_decay=False`) — dedup/conflict must still find an old near-duplicate, else a
    stale fact silently re-enters as a new row.
  - **`as_of` point-in-time recall** — decay relative to `now()` would distort history;
    suppressed when `as_of` is set.
- **Default ON.** Only aged facts are affected; the factory wants current knowledge to win.

## Storage impact

None — reuses `facts.created_at`; the decay is an inline SQL factor on the score. No new
table/column/migration.

## Red-spec eval

`knowledge/evals/cases/matt/recency_decay_stale_loses/` + check
`retrieval_prefers_recent_over_stale`: seed a stale fact (more query-similar, backdated
~400d) and a recent fact (current truth); assert the recent one ranks above the stale one.
RED before (similarity wins regardless of age); GREEN with the decay factor.

## Open questions / follow-ups

- **Confirmation-refresh ("not confirmed in N runs"):** a `last_confirmed_at` bumped by
  `record_outcome(success=True)` would let a re-validated old fact reset its recency.
  Deferred (needs a column); v1 decays on `created_at` only.
- **Half-life tuning:** 90d is a starting point; expose as a per-call/param knob if needed
  (overlaps H7 retrieval-tuning).
- **Keyword branch:** decay applies to the cosine branch; the down-weighted BM25 branch is
  left as-is for v1 (it still inherits recency via the fused cosine ranking).
- **In-memory `VectorGraph`:** Postgres-first (matches real usage + the eval); add parity
  if a component case needs it.
