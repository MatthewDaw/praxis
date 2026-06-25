# Proposal: Derivation Edges / Typed Relations (gap H5)

**Status**: Draft · **Raised**: 2026-06-25 · **Owner**: TBD
**Source**: agent-factory gap **H5** (`agent_factory/docs/praxis-gaps.md`) — the second
item in the compounding-loop cluster, after **H1** (outcome/trust feedback, shipped).

---

## Problem

H1 gave each fact a mutable utility score updated by verified success/failure. But that
signal is **local to one fact**. The compounding loop the reference model wants is:

> "learning L was derived from facts F1, F2 (+ a PRD slice S); when F1 flips or its utility
> craters, find every downstream learning that is now suspect."

Today Praxis cannot answer "what is downstream of F?" There is no derivation provenance and
no traversal, so a bad source fact silently poisons everything derived from it with no way to
trace the blast radius. That is the H5 gap.

---

## What already exists (don't rebuild)

The edge substrate is already here — H5 is **producers + traversal + propagation**, not new
storage.

- **`fact_edges` table** — `(org_id, user_id, [cache_key], src_id, dst_id, kind)`, with the
  `kind` column a free-form string. Mirrored by `cached_fact_edges`.
- **Edge CRUD on `PostgresVectorGraph`**
  ([knowledge/knowledge_graph/knowledge_graph_variants/postgres_vector_graph.py](../../knowledge/knowledge_graph/knowledge_graph_variants/postgres_vector_graph.py)):
  - `add_edge(src, dst, kind="contradiction")` — `INSERT ... ON CONFLICT DO NOTHING` (≈971)
  - `remove_edge`, `flip_edge_kind` (used to resolve `contradiction` → `contradicted_by`)
  - `all_edges(kind=None)` (≈1016), `active_edges()` (≈768)
- **Existing kinds in use:** `contradiction`, `contradicted_by`, `supersedes` (the latter
  written by the temporal/overwrite path, ≈395).

So new kinds (`derived_from`, `depends_on`, `fix_resolves_error`) need **no schema change** —
`add_edge(L, F, "derived_from")` already persists. What's missing is everything *around* it.

---

## The gap, precisely (three missing pieces)

1. **No producer.** Nothing ever creates a `derived_from` edge. The write/ingest/promotion
   paths don't record which existing facts (or which PRD slice) a new learning was derived
   from. `add_edge` is only called internally for `contradiction`/`supersedes`.
2. **No traversal.** `all_edges(kind)` returns a flat list; there is no "give me the
   transitive descendants of fact F along `derived_from`" query. Finding the blast radius of a
   flipped fact requires graph traversal the API doesn't offer.
3. **No propagation hook.** When a fact is invalidated (state → `rejected`, a contradiction
   loss, or — post-H1 — its utility craters), nothing walks `derived_from` to flag/re-score the
   learnings built on it. They stay active and fully-trusted.

---

## What I want to build

### 1. Record derivation provenance — *the producer*

A write-time way to declare "this new fact was derived from these source fact ids." Minimal,
additive surface mirroring H1's `tabular=`/`record_outcome` style:

- **Graph API:** `PostgresVectorGraph.write(..., derived_from: list[str] | None = None)` — after
  the fact is persisted (we already have `decision.added_fact_id` from H1), insert one
  `derived_from` edge per source id (`add_edge(new_id, source_id, "derived_from")`). Also a
  standalone `record_derivation(new_id, source_ids)` for after-the-fact wiring.
- **Source-slice provenance** (the "+ PRD slice S" case): a derivation may cite a non-fact
  source (a PRD line). Store that as the existing `Fact.source`/`meta` on the learning rather
  than an edge (edges are fact→fact). The edge layer is for fact→fact derivation.
  *Known limitation:* because a PRD slice is not a node, stale-propagation (§3) cannot reach
  learnings derived from a *changed* PRD line — only fact→fact invalidation traverses. Acceptable
  for v1; finding PRD-slice dependents would be a `meta`/text query, not a graph walk.
- **Who calls it:** out of scope for the Praxis change — the *factory loop* supplies the source
  ids when it writes a learning (it knows what context it retrieved). Praxis just needs to
  accept and store them. (Same split as H1: harness produces the signal, Praxis stores/uses it.)

### 2. Traverse derivations — *the query*

**Edge direction (pin this — it is the easiest thing to get backwards).** The producer writes
`add_edge(L.id, F.id, "derived_from")`, i.e. `src = L` (the learning), `dst = F` (the basis):

```
L  --derived_from-->  F          "L was derived from F"
(src)                (dst)
```

So the **dependents of F** (the learnings that cite F, the things suspect when F flips) are the
rows where **`dst_id = F`**; the dependent is the `src_id`. Transitive dependents are found by
recursing on those `src_id`s as the next `dst`. (Do **not** match `src_id = F` — that finds what
F itself was derived *from*, i.e. F's bases, the opposite direction.)

- **`dependents(fact_id, kind="derived_from", max_depth=…) -> list[Fact]`** on the graph
  (renamed from `descendants` — "descendants" is ambiguous with `derived_from`). Recursive CTE
  over `fact_edges`, **anchored `WHERE dst_id = :fact AND kind = 'derived_from'` selecting
  `src_id`**, recursing `JOIN fact_edges e ON e.dst_id = <previous src_id>`. Cycle-guarded
  (track visited ids), tenant-scoped, depth-bounded.
- **`stale_derived() -> list[Fact]`** — the headline "what's suspect now?" surface. It returns
  facts carrying a `review:derived-source-invalidated:*` flag (set transitively by the
  propagation hook in §3) — **not** a one-hop query over rejected sources. Keying on flags is
  what makes it transitive: M (derived from L, derived from F) surfaces even though M's direct
  source L is *flagged, not rejected*. One writer (the hook), one reader (this query).

### 3. Propagate on invalidation — *the hook*

Hook this on the **single `state → rejected` transition** (one chokepoint), not on each of the
three reject callers (contradiction loss in `auto_resolve`, explicit reject, supersession) — wire
it once where the state actually flips, or one path will silently skip propagation. When a fact F
flips to `rejected`, walk its **transitive `dependents(F)` closure** (§2) and **flag each** for
review — do NOT auto-reject them (precision-first; a human/agent decides). Concretely: stamp a
`review:derived-source-invalidated:<source_id>` entry on each dependent (reuse the existing
`flags`/review surface that `contradiction:<id>` already uses) so they appear in the
contradiction/review surface alongside the surfaced-contradiction work (#75). Flagging the full
closure here is what lets `stale_derived()` (§2) stay a simple flag-read and still be transitive.

### 4. Surface it — *the API/MCP layer* (thin)

- HTTP: `GET /derivations/stale` (returns `stale_derived()`), and accept `derivedFrom: [ids]`
  on `POST /insights` and `POST /ingest`.
- MCP: extend `praxis_add_insight` / `praxis_ingest` with an optional `derived_from` arg, and a
  `praxis_get_stale_derivations` tool. (Mirrors the contradiction-surface tools.)

---

## Storage impact

| Piece | Touches | New table? |
|---|---|---|
| `derived_from` / `depends_on` edges | existing `fact_edges` (free-form `kind`) | **No** |
| `descendants` / `stale_derived` traversal | read `fact_edges` (recursive CTE) | No |
| review flagging on invalidation | existing `flags` mechanism | No |
| optional index on `fact_edges (kind, dst_id)` | index only — speeds the `dst_id = :fact` dependents lookup (§2) | index, not a table |

No new database, no new table. The one *optional* addition is an index to keep the recursive
traversal fast on large tenants.

---

## The red-spec eval (proves the failure point)

`knowledge/evals/cases/matt/derivation_stale_source/` + a new deterministic check
`derivation_surfaces_stale_when_source_invalidated`:

1. Seed a **source** fact F and a **learning** L, both `active`, in an isolated tenant.
2. Record the derivation: `add_edge(L.id, F.id, "derived_from")` (storage already supports it).
3. Assert `dependents(F, "derived_from")` returns L — pure traversal, green after step-1 of
   Sequencing.
4. Invalidate F **through the real reject path** (so the §3 propagation hook fires), then assert
   `stale_derived()` returns L — green only after the propagation hook (step-3 of Sequencing).
   (For a transitive check, add M with `derived_from` L and assert M surfaces too.)

**RED today:** `dependents()`/`stale_derived()` don't exist (the check treats
`AttributeError`/empty as a fail) — F is rejected but L stays active and untraced. The two asserts
flip green at different sequencing steps (traversal first, hook-backed `stale_derived` last), which
makes this a graduated gate, not all-or-nothing. (Ships alongside this proposal.)

---

## Sequencing

1. **`dependents()` traversal** + the eval — makes the red-spec runnable; the traversal assert
   (eval step 3) goes green here. Smallest, highest-signal first.
2. **Producer (`derived_from` on write / `record_derivation`)** — so real derivations get
   recorded, not just test-injected edges.
3. **Propagation hook + `stale_derived()` flag-read** — flag the transitive `dependents` closure
   at the single `state → rejected` chokepoint; wires H5 into the reject path and the #75 review
   surface. The `stale_derived()` assert (eval step 4) goes green here.
4. **HTTP + MCP surface** — expose to the factory loop.

Steps 1–3 are the Praxis core; step 4 is the thin integration layer (same pattern as H1/#75).

## Acceptance criteria

- [ ] A `derived_from` edge can be recorded at write time and via `record_derivation`.
- [ ] `dependents(F, "derived_from")` returns the transitive learnings built on F, matching
      `dst_id = F` (cycle-safe, depth-bounded).
- [ ] Rejecting a source — via the single `state → rejected` chokepoint — flags its transitive
      `dependents` closure for review (not auto-rejected).
- [ ] `stale_derived()` returns the flagged-stale learnings (transitive, flag-read); the red-spec
      flips RED → GREEN.
- [ ] No new table; no regression in the existing edge/contradiction suites.

**Deferred to v2 (out of v1 acceptance):**
- [ ] Post-H1 tie-in: a source whose utility falls below a floor (not just `state=rejected`) also
      flags its dependents stale.

## Open questions

- **Invalidation trigger for H1 tie-in:** *resolved* — v1 triggers only on `state=rejected`; the
  utility-floor trigger is deferred to v2 (see acceptance). Open sub-question: what floor value,
  decided when v2 is scoped.
- **Auto-reject vs flag-only:** confirm descendants are flagged for review, never auto-rejected
  (precision-first, consistent with the surface-mode work).
- **Depth bound:** cap transitive depth (and dedupe) to bound cost on dense graphs.
- **`depends_on` / `fix_resolves_error`:** ship `derived_from` first; add the other typed kinds
  once the traversal+surface pattern is proven (same machinery, different `kind`).
