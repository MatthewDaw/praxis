# Proposal: active-fact retrievability for the application eval suite

**Owner:** Dominic Antonelli — eval harness / knowledge graph
**Status:** Implemented — Option A (the `ingest_state` axis) shipped: `EvalCase.ingest_state` (default `proposed`), honored in `run._seed_knowledge` + the seed-cache signature, with the 14 `matt/applications/*` cases set to `ingest_state: active`. The application suite now grounds (empty-context → 11.9k-char reference). Follow-up: the grounding-aware rubric judge ([`2026-06-23-grounding-aware-rubric-judge.md`](../2026-06-23-grounding-aware-rubric-judge.md)).
**Date:** 2026-06-23
**Scope:** how the eval harness seeds `seeded_insight.via_ingestor` knowledge for cases that need it
to be *retrievable* — specifically the `matt/applications/*` suite. Eval-infra only; the production
serve/ingest path is already correct (passive add → `proposed`, human approval → `active`).
**Relates to:** the `model-robust-recall-policies` spec FR-030/SC-013 (this is the **second** of two
prerequisites), and [`2026-06-22-deterministic-ingestion-cassette.md`](2026-06-22-deterministic-ingestion-cassette.md)
(the **first** prerequisite, now implemented in `specs/002-deterministic-ingestion-cassette/`).

> The application eval suite runs the agent against an **empty knowledge prompt**. Its background
> docs are ingested as `proposed`, but retrieval (`read`/`search`) is gated to `active`, so
> `reader.read()` returns nothing and the agent writes ungrounded answers. This is the second
> FR-030/SC-013 prerequisite. The fix is small; the decision is *which mechanism* turns retrieval on.

---

## 1. Problem

`_seed_knowledge` (`knowledge/evals/run.py`) seeds two channels with different lifecycle states:

- `seeded_insight.direct_to_graph` → `graph.write(text, state="active")` (pre-curated, retrievable)
- `seeded_insight.via_ingestor` → `ingestor.ingest(text, state="proposed")` (passive distillation, staged)

Retrieval is gated to `active` facts (`VectorGraph.read`/`search`, mirrored in Postgres). The
`matt/applications/*` cases seed their entire background (resume, LinkedIn, degree, Gauntlet page)
through `via_ingestor` — because the whole point is to exercise the real ingestion distiller
(`ingest_model`, now cassette-backed). So every application case stores `proposed` facts that the
reader **cannot surface**.

**Measured (2026-06-23, offline):** for `matt_hightouch_complex_ai_products_0_to_1`, after the
harness's `proposed` ingest the graph holds **116 facts** but `reader.read(seed_prompt)` returns
**0 chars**; seeding the *same* docs `active` returns **11,910 chars** containing `praxis` and
`databricks`. Both runners inject `reader.read()` into the prompt (`OpenRouterRunner` system message;
`ClaudeCodeRunner` `--append-system-prompt`), so the gap is **backend-independent** — the agent gets
an empty knowledge prompt on every backend and invents generic prose.

**Symptom:** application cases pass checks that pin *generic* tokens (`embedding`, `react`, `sql` —
any plausible answer says them) and fail checks that pin *specific* seeded facts (`praxis`,
`databricks`, `dbt`, `markov`, `bentoml`). In the last full `--structured` run: 6 pass / 8 fail,
split exactly on that line. A model upgrade (gpt-4o-mini → gpt-4.1-mini) did **not** help — the
context is empty regardless of model.

## 2. What this is NOT

- **Not a production bug.** The serve path is already correct: passive distillation stages
  `proposed`, human approval ingests `active` (`serve/app.py:420`), and a `promote` transition exists
  (`serve/postgres_store.py:125`). Retrieval-gating-to-`active` is the intended, shipped behavior.
- **Not a model/cost problem.** That was a red herring; see §1.
- **Not a change to the gating contract.** `proposed`/`decayed` must stay out of retrieval — the
  reader-cutoff `_before` controls and the dedup/conflict component cases *depend* on that gating.
  Whatever we choose must keep `proposed`-hidden the default.

## 3. The decision: how to make application knowledge retrievable

Four mechanisms were considered. They differ in **where** the change lives and **what** it models.

### Option A — `ingest_state` case axis (seed `active` directly)  ← recommended

Add `ingest_state: Literal["proposed","active"] = "proposed"` to `EvalCase`; `_seed_knowledge`
passes it to `ingestor.ingest(state=case.ingest_state)`. The `matt/applications/*` cases set
`ingest_state: active`; everything else defaults to `proposed` and is unchanged.

- **Pro:** smallest surgical change (one field + one call-site + the generator); it is the spec's
  own suggested shape (001 Known-gaps line 185); it has direct production precedent
  (`serve/app.py:420` ingests `active` for human-gated knowledge); it still exercises the real
  distiller (the `via_ingestor` path) *and* active-gated retrieval; semantically correct — an
  applicant's real resume **is** established/approved background, not a pending candidate.
- **Con:** seeds `active` from the start, so it does not exercise the `proposed → active`
  *transition*. (The application suite has no reason to — that's a separate concern; see Option B.)

### Option B — promotion step (model the `proposed → active` transition)

Seed `proposed` as today, then run a promotion before reading. Requires a graph-level
`promote()`/activate API (none exists on `VectorGraph`/`InMemoryGraph` today — promotion currently
lives in the Postgres *store*'s candidate workflow, not the graph the evals use), plus a harness call.

- **Pro:** exercises the real candidate→promote lifecycle end-to-end.
- **Con:** new cross-implementation graph API for machinery the application suite doesn't need to
  *test*; YAGNI. Testing promotion itself is a distinct candidate-workflow eval, not application
  grounding. Heaviest option for the least marginal value here.

### Option C — reader/retrieval state-scope (surface `proposed` at read time)

Add a per-case reader scope (e.g. `reader_state_scope: active|all`, leaning on the existing
`search(state=None)`).

- **Pro:** no seeding/state change; literally "make them retrievable."
- **Con:** semantically wrong — it makes the agent read *un-approved* facts, which production never
  does; it muddies the active-gating contract the other cases rely on; the eval would no longer
  reflect real retrieval behavior. Rejected.

### Option D — seed via `direct_to_graph` (active) instead of `via_ingestor`

- **Con:** bypasses the ingestion distiller these cases exist to exercise (and that 002's cassette
  backs). Explicitly rejected in the 001 spec. Non-starter; listed for completeness.

**Recommendation: Option A.** It matches the spec's suggestion and a production precedent, is the
minimal change, keeps `proposed`-hidden as the default, and models the correct semantics
(established background). Options C/D are rejected on correctness; Option B is deferred as its own
(promotion-workflow) concern.

## 4. Design sketch (Option A)

```text
EvalCase:
  ingest_state: Literal["proposed", "active"] = "proposed"   # NEW; via_ingestor seeding state

run._seed_knowledge(case):
  for text in direct_to_graph: graph.write(text, state="active")          # unchanged
  for text in via_ingestor:    ingestor.ingest(text, state=case.ingest_state)   # was hard-coded "proposed"

cases/matt/applications/_generate.py:
  case["ingest_state"] = "active"     # regenerate the 14 YAMLs
```

- `_seed_signature` (the optional seed cache) **must** include `ingest_state` — two cases differing
  only in seed state must not share a cached reader. One-line addition.
- Capability gating, the ingestion cassette, and embedding fixtures are all unaffected (state is a
  write-time attribute; the distilled text and its key are identical).
- Component cases (`graph_reader`, `ingestion`, dedup/conflict) keep the `proposed` default, so the
  gating they test is preserved.

## 5. Acceptance test (already in hand)

This proposal ships with its own red→green suite: the application cases whose checks pin **specific**
seeded facts are RED today and MUST go GREEN once `ingest_state: active` lands —
`matt_hightouch_{complex_ai_products_0_to_1, data_warehouse_experience, education_background,
backend_architecture_scaling, agentic_systems, production_llm_pipeline, location_and_visa}` and
`matt_sekai_production_ml_pipelines` (keywords `praxis`, `databricks`/`dbt`, `markov`, `bentoml`,
`utah`, …). Generic-keyword cases already pass and don't prove grounding. With this prerequisite +
002's deterministic ingestion both landed, **FR-030/SC-013 become measurable** for the first time.

## 6. Risks & open questions

- **Write policy on `active` seeds.** Redactor/Deduper/ConflictFlagger run during seeding regardless
  of state, but active facts are now retrievable, so the dedup recall gate operates on them. Verify
  the application cases still seed cleanly (no unexpected merges/decays) after the flip — a quick
  offline `_build_trio_for` + seed check, like the one that proved the gap.
- **Per-case vs default.** Keep the default `proposed`; only application (and future
  "established-background") cases opt into `active`. Do not flip the global default.
- **Naming.** `ingest_state` matches the 001 spec's wording and the `SeedState` type already in
  `write_policy_def.py`. Reuse that type rather than a new literal.
- **Out of scope:** a promotion-workflow eval (Option B), any production change, and full
  end-to-end determinism of the application agent + judge (still nondeterministic by design).
```
