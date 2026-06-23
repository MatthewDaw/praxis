# Phase 0 Research: Deterministic Ingestion Cassette

All Technical Context items are resolved (no NEEDS CLARIFICATION). The source proposal
(`docs/proposals/2026-06-22-deterministic-ingestion-cassette.md`) and the `/speckit-clarify`
session settled the open decisions; this file consolidates them.

## R1 — Cassette shape: mirror `CachedEmbedder`, one layer up

**Decision**: A `str → str` replay cassette keyed `sha256(ingest_model + "\n" + raw_input) →
output_text`. Hit → replay; miss + `allow_compute` (key present) → call the live ingest LLM,
record, save; miss + recording disabled → **loud `RuntimeError`** with a refresh instruction.

**Rationale**: This is exactly the `CachedEmbedder` contract (`knowledge/llm/embedder_variants/cached_embedder.py`:
sha256(`model_id\ntext`) key, `allow_compute` gate, loud miss) applied to a text value instead of a
vector. Proven, committed-fixture-friendly, model-keyed so a model swap is a clean miss.

**Alternatives considered**: temperature=0 alone (already the default and insufficient — backend
nondeterminism persists); a record/VCR HTTP-level cassette (heavier, couples to the HTTP client,
replays transport not semantics); no caching + accept nondeterminism (the status quo this fixes).

## R2 — Wiring: wrap the ingest callable in `run._ingest_llm_for`

**Decision**: `_ingest_llm_for(case, llm)` returns a `str → str` callable when `ingest_model` is
set; wrap that callable in the cassette there, parallel to how `_eval_embedder(case)` wraps `live`
in `CachedEmbedder` for the `cached` axis. `PromptIngestor` is untouched — it still receives a
`str → str` callable (FR-006).

**Rationale**: Single, localized wiring point; keeps case authoring unchanged; mirrors the
embedder wiring symmetrically so the two cache layers compose.

**Alternatives considered**: caching inside `PromptIngestor` (couples production ingestion to eval
infra — rejected, FR-012 keeps this eval-only); a new case axis to opt in (unnecessary — presence
of `ingest_model` already scopes it, and a committed cassette/key gates availability like the
embedding cache).

## R3 — Regenerator + refresh ordering

**Decision**: `knowledge/evals/ingestion_cache.py --refresh` mirrors `embed_cache.py`: with a key,
delete the model's cassette, re-run every `ingest_model` case so the recorder captures exactly the
inputs those cases distill, commit. The documented two-step refresh is **ingestion cassette first,
embedding cache second** (FR-009) — record the text, *then* embed the now-stable strings.

**Rationale**: Starting from empty drops orphaned keys (same as `embed_cache.py`). The ordering is
load-bearing: embeddings key on text, so embedding before the text is fixed would cache
soon-stale vectors.

**Alternatives considered**: a single combined refresh command (hides the ordering dependency and
the two distinct fixtures; rejected for clarity/debuggability).

## R4 — Flip all `ingest_model` application cases to `cached`

**Decision**: Flip every `matt/applications/*` case that uses `ingest_model` from `embedder: live`
to `embedder: cached` in one pass (clarify Q2). Accept the committed distilled-text + embedding
fixture footprint; mitigate with the existing packed vector codec, not by subsetting which cases
flip.

**Rationale**: Full coverage makes the whole application suite's graph-construction layer
deterministic + free on replay; the packed codec already keeps embedding fixtures compact.

**Alternatives considered**: prioritized subset first (the recommendation) — owner chose full
coverage; revisit only if the footprint proves prohibitive (not a goal here).

## R5 — Unify the four cassette surfaces later, not now

**Decision**: Ship `IngestionCassette` as a near-copy of `CachedEmbedder`. Do **not** build a
unified keyed-replay abstraction yet, even though four surfaces (embeddings, ingestion, merge
verdicts, conflict verdicts) now share `sha256(model + payload) → result`.

**Rationale**: Extract the common surface once three concrete instances exist and the shared shape
is proven (it now does, post-001) — but extraction is its own refactor with its own value codec
abstraction; doing it speculatively inside this feature violates YAGNI and bloats scope.

**Alternatives considered**: build the abstraction first and parameterize by codec (premature —
the proposal explicitly recommends against; deferred to a follow-up refactor).

## R6 — Partial determinism, stated honestly

**Decision**: Scope the determinism claim to the ingestion → embedding → graph-construction layer.
The live Claude Code agent and the judge remain nondeterministic; this is **not** "the application
suite runs in CI" (FR-011).

**Rationale**: Overselling would mislead the suite's consumers. The real wins are cost, reproducible
graph state, attributable measurement, and unlocking deterministic component cases — all true
without claiming end-to-end reproducibility.

## R7 — Relationship to FR-030/SC-013 (and the second prerequisite)

**Decision**: This feature is **prerequisite #1** of two for the `model-robust-recall-policies`
spec's deferred FR-030/SC-013. Prerequisite #2 — marking distilled facts retrievable so the
active-gated reader surfaces them (`ingest_state: active`, after main's active-fact gating) — is
**out of scope** and the **explicit next follow-up** (clarify Q1). Shipping 002 alone does not, by
itself, fully unblock FR-030/SC-013.

**Rationale**: The two prerequisites are independent concerns (eval determinism vs. gating
integration); coupling them bloats scope. SC-007 keeps its "subject to the separately-tracked
prerequisite" caveat so the partial unblock is honest.
