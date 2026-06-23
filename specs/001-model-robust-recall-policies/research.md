# Phase 0 Research: Model-Robust Recall Policies

Decisions consolidated from the three source proposals, the spec clarifications, and the empirical probes run during specification. Each item: **Decision / Rationale / Alternatives considered**.

## R1 — Read-path cutoff shape (P1)

**Decision**: `floor → relative → cap`. Apply `abs_floor` (existence), then keep hits `>= rel_ratio * top_score` (shape), then cap at `top_k` (volume), operating only on existing `SearchHit.score`s — no second model call.

**Rationale**: The relative step is model-robust (self-calibrating per query/model); the floor is the only thing that lets negative-control queries return nothing; the cap bounds volume. Matches proposal §5.

**Alternatives**: Pure `top_k` (can't drop irrelevant, never empty); pure absolute threshold (model-pinned, brittle — today's `min_score=0.35`); gap-based `autocut` (parameter-free but degrades on smooth curves) — kept as a possible later refinement for the middle step, not the first cut.

## R2 — Relative-fraction vs gap for the middle step

**Decision**: Relative-fraction (`rel_ratio`) for v1; gap/`autocut` deferred.

**Rationale**: One predictable knob, no smooth-curve failure mode. The floor and cap stay regardless, so gap can swap in later without disturbing the contract.

**Alternatives**: `autocut` (Weaviate) — documented production approach but has a known smooth-curve limitation; not worth its complexity for the first cut.

## R3 — Fixed `top_k` is insufficient alone (empirical)

**Decision**: The relative ratio (not a larger fixed `top_k`) is the primary precision knob; `top_k` is only a backstop.

**Rationale**: A 2026-06-22 probe enabling `reader: retrieving` (`top_k=8`) across all 14 application cases flipped **4 PASS→FAIL**, every one because the asserted fact fell outside the top-8 window. Broad questions ("walk me through your education") legitimately need more than 8 facts; narrow ones need fewer. A single global `top_k` serves neither — the relative cutoff adapts (keep many similarly-relevant facts, few when one dominates). This is direct evidence for R1's relative step over a tuned cap.

**Alternatives**: Bump global `top_k` higher — still wrong for narrow queries (re-admits noise) and arbitrary.

## R4 — Numeric defaults (calibration task)

**Decision**: Ship coarse, model-documented defaults calibrated against the committed `text-embedding-3-small` cache: `abs_floor ≈ 0.30`, `rel_ratio ≈ 0.75`, `top_k = 8`, dedup/conflict `recall_floor ≈ 0.45`. Document the model alongside the values; recompute on model change.

**Rationale**: Grounding data (proposal §2): relevant facts cluster 0.45–0.52, distractors ≤0.27 (clean 0.18 gap); paraphrases ~0.65, distinct ideas ~0.31; no-match top ~0.2. The floor sits in the unrelated band (coarse, not on the separating line); `recall_floor` is deliberately low (high-recall, forgiving). Calibration is a verification step against the committed cache, not a blocker on structure.

**Alternatives**: Per-case tuned values — rejected as fake passes (the whole point of the feature). Normalized floor — existence is inherently absolute, so a normalized floor is not meaningful.

## R5 — Two-stage dedup: recall gate + LLM merge-judge (P2)

**Decision**: Keep exact-match short-circuit; replace `score >= threshold` with a loose `recall_floor` candidate gate + `MergeJudge` (LLM) that answers `{same_lesson, keep_id}` and selects the **verbatim** survivor. Rename `Deduper.threshold` → `recall_floor`.

**Rationale**: Measured paraphrase similarity (~0.65) is far below the near-exact 0.95; and optimal paraphrase thresholds are model-dependent (0.33–0.87 across models), so no single threshold is portable. Precision belongs in the judge, not a threshold — and only a judge can pick the verbatim survivor (`skills_merge_dedup` requires this; LLM *distillation* that rewrites is explicitly NOT the green path). Bi-encoder→cross-encoder/LLM is the high-precision standard.

**Alternatives**: Calibrate one threshold (treadmill, brittle at boundary, can't pick survivor); cross-encoder verifier (cheaper/deterministic but adds a model dep, can't pick verbatim survivor or explain) — kept as a cost-driven fallback only (resolves dedup proposal Q4 + unified §5.4).

## R6 — Offline determinism: verdict cassette (P2/P3)

**Decision**: A committed, model-keyed JSON cassette mirroring `CachedEmbedder`: `sha256(judge_model + payload) -> verdict`, replay offline, record on miss only with a key, **loud miss** on a stale fixture, **skip** (graceful) when no key and no cassette. One implementation (`verdict_cassette.py`) parameterized for merge (`{same_lesson, keep_id}`) and conflict (`{contradicts, target_id}`) verdicts.

**Rationale**: The judges are nondeterministic and keyed exactly like embeddings — reuse the proven pattern so dedup/conflict evals run free and deterministically in CI. Graceful-skip matches today's `ConflictFlagger`.

**Alternatives**: Live judge in CI (nondeterministic, costly, flaky); per-cassette bespoke classes (needless duplication — one keyed-replay surface suffices).

## R7 — Embed once per write (FR-015) — vector-aware write path (P3)

**Decision**: `VectorGraph.write` computes the incoming embedding once; a single `most_similar`-style candidate-recall pass uses it; both judges consume that candidate set; `_add` persists that same vector. One shared `recall_floor`. Merge runs before conflict; a merged dup skips the conflict check.

**Rationale**: Today the same text is embedded 2–3× per write (Deduper search + ConflictFlagger search + store), and under `embedder: live` each is a real API call — confirmed during the session. Threading the vector is the only way to guarantee "exactly once" (SC-007) and is the natural shape for the FR-015 unification (single recall pass, single floor — kills the inconsistent 0.95 vs 0.6 floors).

**Alternatives**: Per-call memoization of `embed_one(text)` — collapses duplicate searches but leaves the store embed and is a band-aid; doesn't model the shared candidate set.

## R8 — Implicit-contradiction recall: gated Tier-B experiment (P3)

**Decision**: Add write-time aspect/topic tags (controlled vocabulary) as a second recall signal for the **conflict path only**: candidates = `cosine-kNN ∪ same-tag`. Ship behind a kill/keep gate: build a small implicit-contradiction eval set, measure tag co-assignment recall + end-to-end flag rate; keep only if it clears.

**Rationale**: The implicit case (~0.454 cosine, e.g. "prioritize raw performance" vs "readability over micro-optimizations") sits just above "distinct ideas" (0.31) — unreachable by lowering the floor. A different recall key is the only mechanism. Multi-key blocking is the mature entity-resolution approach. But no source shows tag blocking actually catches the *implicit* case, so it ships unproven, with an explicit gate.

**Alternatives**: HyDE/counter-claim generation — directly tested in the literature and failed catastrophically (semantic collapse); NegEx negation-cue filter — needs explicit negation markers the 0.454 case lacks; better embedder — the pair has genuinely low topical proximity. All ruled out (unified §1).

## R9 — Tier-C residual: documented, not built (P3)

**Decision**: Name the residual (some implicit contradictions remain unrecalled, field-wide unsolved); the honest backstop is an offline/async batch compaction pass. Document only; do **not** build in this feature.

**Rationale**: No write-time silver bullet exists; only 3 of 8 human-evaluated systems scored non-zero on contradiction at all. Contradictions surface to a review dashboard, so they tolerate batch latency. Dedup does not need it (write-path recall already solved).

## R10 — Eval cluster reconciliation (cross-cutting)

**Decision**: Per spec FR-024..FR-029: resolve `lost_in_middle_reader` (drop the PROVISIONAL note, `abs_floor=0` to isolate the relative cutoff); convert `reader_returns_all` to a `_before` control + add an `after` (unless redundant); redesign `scattered_multifact` as two recall-under-noise versions (far-only expected-pass; near-only provisional); make negative-control/no-leak cases floor tests; flip `ingestion_merge_near_dupes` + `skills_merge_dedup` to PASS via the real merge. Mechanism-isolation tests neutralize the other mechanism (floor=0 or ratio=0) per the spec's isolation matrix.

**Rationale**: Tests assert real shipped behavior, never a per-case-tuned constant ([[eval-assert-actual-behavior]]); before/after naming per [[eval-before-after-naming]].

**Alternatives**: Per-case threshold overrides to force green — explicitly rejected as fake passes.

## R11 — Application-suite validation needs deterministic ingestion (prerequisite)

**Decision**: Treat the deterministic-ingestion cassette as a sequenced **prerequisite** for FR-030/SC-013; do not rely on `matt/applications/*` pass/fail to validate write/read-policy changes until it lands. Use component-level cases as the primary verification surface here.

**Rationale**: A 2026-06-22 probe showed the live `gpt-4o-mini` splitter changes which facts exist run-to-run (a fact asserted by a check was in the graph one run, gone the next), so application-suite swings can't be attributed to a policy change. Tracked in the cassette proposal; out of scope here.

**Alternatives**: Validate against the application suite as-is — produces unattributable noise.
