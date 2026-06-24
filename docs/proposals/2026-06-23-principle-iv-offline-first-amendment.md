# Follow-up: reconcile Constitution Principle IV (Offline-First) with the Postgres-only server

**Status**: Open follow-up · **Raised**: 2026-06-23 · **Source**: `/speckit-analyze` finding C1 on
[specs/003-fact-rejection-lifecycle](../../specs/003-fact-rejection-lifecycle/plan.md)

## Problem

Constitution v1.0.0 **Principle IV (Offline-First & Graceful Degradation)** states (MUST):

> A missing Postgres DSN MUST fall back to the JSON/in-memory path; … a missing graph backend
> MUST NOT block candidate review.

This no longer holds. Commit `dbf60d9` ("collapse knowledge graph onto a single facts spine")
deleted the JSON candidate store (`CandidateStore` / `PostgresCandidateStore`) and made
`create_app()` open a single shared connection that **requires a resolvable DSN**
([knowledge/serve/app.py](../../knowledge/serve/app.py)). The dashboard candidate surface, graph
view, MCP context, and Contradictions tab all read `facts` via `FactsCandidates`, which has no
offline backing.

The 003 feature (REJECTED state + retained-contradiction lifecycle) **inherits** this constraint;
it does not introduce it. Per the analyze Constitution-Authority rule, a standing conflict with a
MUST principle must be resolved explicitly rather than silently accepted — hence this follow-up.

## Decision (003 scope)

Path **(a)**: do **not** re-add an offline path inside the 003 feature. Re-building an offline
facts + edge store solely to satisfy Principle IV here is a large, out-of-scope rebuild of what the
refactor intentionally removed. 003 proceeds Postgres-only; the deviation is recorded in
[plan.md](../../specs/003-fact-rejection-lifecycle/plan.md) Complexity Tracking and tracked here.

## Options for reconciling the constitution (to be decided separately, via `/speckit-constitution`)

1. **Amend Principle IV** to reflect reality: the *server / persistent graph* is Postgres-backed
   and may hard-require a DSN; the offline-first guarantee is scoped to the surfaces that still have
   a credential-free path (e.g. the frontend mock/fixture provider, deterministic LLM/embedder
   fallbacks). This is the lowest-friction option and matches the post-refactor architecture.
2. **Restore an offline facts/edge store** behind `FactsCandidates` so the candidate review path
   works without Postgres again. Honors the principle as written but reverses a deliberate refactor
   decision; significant effort.
3. **Narrow Principle IV's MUST to a SHOULD** for the persistent graph specifically, keeping MUST
   for the frontend demo path and LLM/embedder degradation.

## Recommendation

Option 1 (amend + scope the principle to the surfaces that retain an offline path). Track as a
constitution amendment PR (governance §Versioning → likely MINOR: materially expands/clarifies a
principle's scope). Until then, 003 and any other Postgres-only work carries the documented
deviation.

## Action items

- [ ] Open a constitution-amendment PR (`/speckit-constitution`) implementing Option 1 (or chosen
      alternative), bumping the version per the governance policy.
- [ ] Once amended, remove the deviation note from
      [specs/003-fact-rejection-lifecycle/plan.md](../../specs/003-fact-rejection-lifecycle/plan.md)
      Complexity Tracking and close this follow-up.
