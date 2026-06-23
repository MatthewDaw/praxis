# Feature Specification: Model-Robust Recall Policies for the Knowledge Graph

**Feature Branch**: `001-model-robust-recall-policies`

**Created**: 2026-06-22

**Status**: Draft

**Input**: User description: "docs\proposals\2026-06-22-reader-cutoff-policy.md, docs\proposals\2026-06-22-semantic-dedup-recall-gate-llm-judge.md, and docs\proposals\2026-06-22-unified-dedup-conflict-recall.md"

## Overview

Today three knowledge-graph behaviors are decided by hardcoded cosine-similarity numbers that are pinned to one embedding model: how the reader decides which retrieved facts are relevant, how the write path decides two notes are duplicates, and how it decides two notes contradict. Each number is brittle — it must be re-tuned whenever the embedding model changes, it cannot honestly say "nothing here is relevant," and it cannot recognize that two differently-worded notes mean the same thing or disagree.

This feature replaces those brittle numbers with **model-robust recall policies**: a layered cutoff for the read path, and a two-stage "loose recall gate → precise judge" pattern for the write path. As a direct consequence, the cluster of evaluation cases that currently guess at these behaviors is reconciled to assert the system's real, shipped behavior.

## Clarifications

### Session 2026-06-22

- Q: Is the implicit-contradiction work (Tier B: write-time aspect/topic tags as a second recall signal + dedicated eval set + keep/kill gate) in scope for this feature? → A: Yes — in scope as a **gated experiment**: build it, measure tag co-assignment recall against the keep/kill gate, keep only if it clears; otherwise the implicit cases remain documented XFAIL.
- Q: For the redesigned `scattered_multifact` recall-under-noise test, how near should the distractors be? → A: **Two versions** — a far-only version (clearly-unrelated distractors, expected to pass easily) and a near-only version (loosely-related distractors that probe the relative-cutoff boundary; provisional — may pass or fail, and either outcome is informative).
- Q: How should `reader_returns_all` (which asserts the now-falsified dump-everything behavior) be handled? → A: Convert it to a **`_before` control case** (asserts the old dump-all behavior; XFAIL under the new reader) and add a reasonable **`after` case** asserting the ranking behavior, *unless* that assertion is already redundant with `lost_in_middle_reader` / the redesigned `scattered_multifact`.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Model-robust reader cutoff (Priority: P1)

A maintainer relies on the retrieving reader to surface only the facts relevant to a query. Today the reader keeps facts above a single hardcoded score that was calibrated to one embedding model; it admits clearly-irrelevant facts, cannot return an empty result when nothing matches, and breaks if the embedding model is swapped. The maintainer needs the reader to keep all genuinely-relevant facts, drop the irrelevant-but-retrieved ones, and return nothing when nothing is relevant — without re-tuning a precise number per model.

**Why this priority**: This is the read-path foundation. It is fully self-contained, settles the largest group of evaluation cases, and is a prerequisite for trusting the reader in any downstream use. It delivers value with no dependency on the write-path stories.

**Independent Test**: Can be tested in isolation by running queries against a fixed knowledge graph and asserting that (a) all relevant facts of varying strength survive, (b) retrieved-but-irrelevant facts are dropped, and (c) a query with no good match returns nothing — using the committed deterministic retrieval data, with no live model call.

**Acceptance Scenarios**:

1. **Given** a graph containing two strongly-relevant facts and several weakly-scoring distractors, **When** the reader processes a relevant query, **Then** both relevant facts are returned and every distractor is excluded.
2. **Given** a graph and a query for which no fact is genuinely relevant, **When** the reader processes the query, **Then** the reader returns nothing (no facts are injected downstream).
3. **Given** an aggregation query whose answer depends on several genuinely-relevant facts of differing strengths surrounded by clearly-unrelated (far) distractors, **When** the reader processes the query, **Then** all of those relevant facts survive and every far distractor is dropped (the far-only recall-under-noise version — expected to pass).
4. **Given** the same aggregation query but with loosely-related (near) distractors that sit just below the relevant facts, **When** the reader processes the query, **Then** the reader surfaces exactly the relevant facts; this near-only version is **provisional** — it probes the relative-cutoff boundary and either outcome (pass, or a documented near-miss) is recorded as real behavior, never tuned to force a pass.
5. **Given** the same graph scored by a different embedding model, **When** the reader processes a relevant query, **Then** the same relevant/irrelevant separation holds without changing a precise per-model separating value.

---

### User Story 2 - Semantic deduplication of paraphrased notes (Priority: P2)

A maintainer writes a note that says the same thing as an existing note in different words ("use uv run pytest" vs "run tests with uv run pytest"). Today the write path only merges near-identical text, so paraphrases accumulate as duplicates. The maintainer needs the system to recognize paraphrases as the same lesson, merge them into a single surviving note kept **verbatim** (not rewritten), and still keep genuinely distinct ideas separate.

**Why this priority**: This is the highest-value write-path improvement and is independently shippable. It removes duplicate knowledge accumulation and flips two long-standing known-failing evaluation cases to passing. It depends only on the existing write path, not on Story 1.

**Independent Test**: Can be tested in isolation by ingesting a paraphrase pair and asserting one merged survivor exists verbatim with an incremented observation count, and by ingesting a distinct-idea pair and asserting both survive — replayed against a committed merge-verdict fixture with no live model call.

**Acceptance Scenarios**:

1. **Given** an existing note and an incoming paraphrase of it, **When** the write path processes the incoming note, **Then** the two are merged into one surviving note, the surviving wording is one of the original verbatim notes (not a rewrite), and the merged note's observation count reflects both.
2. **Given** an existing note and an incoming note expressing a genuinely distinct idea, **When** the write path processes it, **Then** both notes are kept (no over-merge).
3. **Given** an incoming note identical to an existing one, **When** the write path processes it, **Then** it is deduplicated by the existing exact-match behavior (unchanged).
4. **Given** the deterministic merge-verdict fixture is missing or stale, **When** the dedup evaluation runs without live-model credentials, **Then** the case is skipped (or fails loudly on a stale fixture) rather than silently producing a wrong result.

---

### User Story 3 - Unified, hardened, and recall-aware contradiction handling (Priority: P3)

A maintainer adds a note that conflicts with an existing one. Today contradiction detection and deduplication run as separate steps with separate, inconsistent thresholds and embed the same text multiple times per write; the contradiction judge parses free-text output fragilely; and contradictions phrased with different vocabulary ("prioritize raw performance" vs "readability over micro-optimizations") are never even surfaced as candidates. The maintainer needs dedup and contradiction to share one candidate-finding pass with consistent behavior, robust structured judgments, deterministic offline evaluation, and a measured, honest attempt at catching implicit (different-vocabulary) contradictions — with the limits of that attempt named explicitly rather than oversold.

**Why this priority**: This unifies and hardens the write path on top of Story 2's machinery and tackles the hardest, field-wide-unsolved recall case. It is valuable but builds on Story 2, so it follows it. The implicit-contradiction portion is an explicitly gated experiment that may be kept or dropped based on measured results.

**Independent Test**: Can be tested by asserting that a single candidate-recall pass feeds both the merge decision and the contradiction decision; that a just-merged duplicate skips the contradiction check; that contradiction verdicts come from structured output replayed against a committed fixture; and that the implicit-contradiction evaluation set reports its recall against a defined keep/drop gate.

**Acceptance Scenarios**:

1. **Given** an incoming note, **When** the write path processes it, **Then** candidate similar notes are found by a single recall pass and that same candidate set is used for both the "same lesson?" and the "contradict?" decisions (text is embedded once per write).
2. **Given** an incoming note that is merged as a duplicate, **When** the write path continues, **Then** the contradiction check is skipped for that note (merge takes precedence over conflict-flagging).
3. **Given** an explicit (negation-style) contradiction pair, **When** the write path processes the incoming note, **Then** the pair is surfaced and flagged as a contradiction via a structured judgment, deterministically replayed from a committed verdict fixture.
4. **Given** the gated implicit-contradiction experiment, **When** its evaluation set of implicit-contradiction pairs is run, **Then** the system reports tag co-assignment recall and end-to-end flag rate, and the experiment is kept only if it clears its predefined gate; otherwise the implicit cases remain documented as a known unrecalled limitation rather than a tuned fake pass.

---

### Edge Cases

- **No relevant facts at all (read path):** A query whose best match is weak must yield an empty result, so nothing irrelevant is injected downstream (the negative-control / no-leak family).
- **Weak-but-relevant facts (read path):** An aggregation query's weakest genuinely-relevant fact must not be cut by the relative cutoff; this bounds how aggressive that cutoff may be.
- **Embedding-model swap:** Read-path separation and write-path recall must survive a model change by adjusting only coarse/forgiving values, never a precise separating line.
- **Live model unavailable (offline / CI):** Both the merge decision and the contradiction decision must replay from committed fixtures; with no fixture and no credentials, the affected evaluation is skipped, and a stale fixture fails loudly.
- **Over-merge risk (write path):** Distinct ideas that happen to be topically near must not be merged; a distinct-ideas guard must catch this.
- **Implicit contradictions with no shared vocabulary and no negation cue:** Acknowledged as field-wide unsolved; if the gated experiment does not clear its gate, these remain an honest, documented miss with an optional offline/batch backstop, not a silently-passing case.
- **Decay/recency-driven cases:** Recency/decay filtering is a separate mechanism and is explicitly unaffected by these recall policies.

## Requirements *(mandatory)*

### Functional Requirements

#### Read-path cutoff (Story 1)

- **FR-001**: The reader MUST apply a layered cutoff to retrieved facts consisting of (1) an absolute existence floor, (2) a relative "keep what is close to the best hit" cutoff, and (3) a volume cap on the number of facts kept, applied in that order.
- **FR-002**: The reader MUST return an empty result when the best-scoring retrieved fact does not clear the absolute existence floor (so a no-relevant-fact query injects nothing downstream).
- **FR-003**: The reader MUST keep all genuinely-relevant facts of varying strength for a relevant query, including weaker-but-relevant facts that an aggregation answer depends on.
- **FR-004**: The reader MUST drop retrieved-but-irrelevant facts that fall well below the best hit, even when they are present in the retrieved set.
- **FR-005**: The cutoff values MUST be defined as the reader's global system contract (its production defaults), not as per-evaluation-case tuning knobs.
- **FR-006**: The reader MUST expose per-case overrides for each cutoff value, used solely to isolate one mechanism by neutralizing the others during testing (not to manufacture a passing result).
- **FR-007**: The system MUST operate the read-path cutoff using only the similarity scores already produced by retrieval, with no additional model call.

#### Write-path deduplication (Story 2)

- **FR-008**: The write path MUST preserve its existing exact-match deduplication as a short-circuit.
- **FR-009**: The write path MUST find duplicate candidates with a loose, high-recall similarity gate whose job is only to surface plausible duplicates (tolerating false positives), not to decide duplication.
- **FR-010**: The write path MUST decide whether two notes record the same lesson using a precise judge, not a similarity threshold.
- **FR-011**: When two notes are judged the same lesson, the system MUST merge the incoming note into the surviving existing note, keep the surviving note's wording verbatim (no rewriting/distillation), and reflect both observations in the merged note's count.
- **FR-012**: When two notes are judged distinct, the system MUST keep both (no over-merge), and a distinct-ideas guard MUST detect over-merge regressions.
- **FR-013**: The system MUST replay merge judgments deterministically from a committed, model-keyed verdict fixture for offline/CI runs, recording new verdicts by key and failing loudly on a stale fixture.
- **FR-014**: When no verdict fixture and no live-model credentials are available, the system MUST skip the semantic-merge step (exact dedup still applies) and the affected evaluation case MUST skip rather than mis-run.

#### Write-path unification & contradiction (Story 3)

- **FR-015**: The write path MUST embed the incoming text at most once per write: a single candidate-recall pass produces one vector, that same vector feeds both the same-lesson (merge) decision and the contradiction decision, AND it is the vector persisted with the stored fact (no separate re-embed at store time). On a write that finds no candidates (e.g. the first write into an empty graph), the path MUST still embed the incoming text at most once and MUST NOT issue a candidate search it knows will return nothing.
- **FR-016**: The write path MUST apply a single shared recall floor across the merge and contradiction paths (replacing the previously inconsistent separate thresholds).
- **FR-017**: The write path MUST evaluate merge before contradiction, and a note that was merged as a duplicate MUST skip the contradiction check.
- **FR-018**: The contradiction decision MUST be produced as a structured judgment (an explicit contradicts flag plus the target note), not by parsing free-text output.
- **FR-019**: The system MUST replay contradiction judgments deterministically from a committed, model-keyed verdict fixture, with the same record/loud-miss/skip-when-no-credentials behavior as the merge fixture.
- **FR-020**: Existing explicit (negation-style) contradiction behavior MUST be preserved, now hardened by structured output, the shared recall pass, and the deterministic fixture.
- **FR-021**: The system MUST include a gated experiment for implicit (different-vocabulary) contradiction recall that adds a second, non-similarity recall signal (controlled aspect/topic tags assigned at write time) unioned into the contradiction candidate set, scoped to the contradiction path only.
- **FR-022**: The implicit-contradiction experiment MUST ship with a defined evaluation set of implicit-contradiction pairs and MUST report the gate metrics (tag co-assignment recall and end-to-end flag rate) on that set. No fixed numeric threshold is pinned in advance: the **feature owner reviews the reported metrics and decides keep or kill** (a human judgment call, surfaced for explicit decision when the experiment runs). The tag key is kept only if the owner judges the gate cleared.
- **FR-023**: If the owner judges the gate not cleared, the corresponding evaluation cases MUST remain documented as a known unrecalled limitation (honest, not a per-case-tuned fake pass), the tag key MUST be dropped, and the system MUST NOT chase a write-time "silver bullet" for the field-wide-unsolved residual.

#### Evaluation reconciliation (cross-cutting)

- **FR-024**: Evaluation cases MUST assert the system's real, shipped behavior under these policies rather than per-case-tuned constants; cases whose behavior is undecided MUST be marked provisional.
- **FR-025**: The reader-dependent evaluation cluster MUST be reconciled to the new read-path policy: the previously-provisional drop-irrelevant case resolves, the dump-everything case is converted per FR-028, the multi-fact case is redesigned per FR-029, and the no-leak cases become existence-floor tests.
- **FR-028**: The `reader_returns_all` case MUST be converted into a `_before` control case asserting the old dump-everything behavior (expected to fail under the new ranking reader), and a corresponding `after` case asserting the ranking behavior MUST be added unless that assertion is already redundant with `lost_in_middle_reader` or the redesigned `scattered_multifact` (in which case the redundancy MUST be noted rather than a duplicate case added).
- **FR-029**: The `scattered_multifact` case MUST be redesigned as a recall-under-noise test in two versions: a **far-only** version (clearly-unrelated distractors, expected to pass) and a **near-only** version (loosely-related distractors probing the relative-cutoff boundary), where the near-only version is marked **provisional** and asserts whatever the calibrated cutoff actually does (pass or documented near-miss), never a per-case-tuned pass.
- **FR-026**: The two paraphrase-deduplication evaluation cases that are currently known-failing MUST become passing because the system actually merges paraphrases (verified against committed verdicts), not because a threshold was tuned.
- **FR-027**: Read-path and write-path policies MUST each be covered by mechanism-isolation tests (one mechanism exercised with the others neutralized) plus an integration test exercising the production defaults together.
- **FR-030** *(Deferred — prerequisite-gated; not satisfied within this feature's tasks)*: Write-path changes (Stories 2–3) MUST be validated against the application eval suite (`matt/applications/*`), which exercises the real write policy end-to-end (`substrate: vector`, `embedder: live`, `ingest_model` set), not only against the dedicated dedup cases — no application case may regress unexpectedly, and any intended behavior change there MUST be reflected in that case's expectations. This validation is only attributable once ingestion is deterministic (see Known gaps & risks); until then the suite's pass/fail cannot be ascribed to a policy change, so the deterministic-ingestion cassette is a **prerequisite** for FR-030/SC-013, sequenced before this validation is relied upon. **This requirement is therefore deferred:** it is intentionally not covered by this feature's tasks and becomes active only after the ingestion-cassette work lands. Until then, component-level cases (mechanism-isolation, dedup, conflict) are the verification surface.

### Key Entities *(include if feature involves data)*

- **Retrieved fact / similarity score**: A candidate fact returned by retrieval with a similarity score (higher = more similar). The read-path cutoff and the write-path recall gate both operate on these scores.
- **Cutoff policy (read path)**: The reader's contract — an existence floor, a relative-to-best ratio, and a volume cap — with per-case isolation overrides.
- **Recall gate (write path)**: A single loose similarity floor that surfaces merge and contradiction candidates without deciding either.
- **Merge verdict**: A structured judgment of whether two notes record the same lesson and which existing note survives verbatim; keyed by model + the note pair; persisted in a committed fixture.
- **Contradiction verdict**: A structured judgment of whether two notes contradict and which note is the target; keyed and fixtured like the merge verdict.
- **Aspect/topic tag**: A controlled-vocabulary label assigned to a note at write time, used as a second recall signal for the implicit-contradiction experiment.
- **Surviving note / observation count**: The note kept after a merge (verbatim) and the count reflecting how many observations it represents.
- **Evaluation case**: A harness case asserting real system behavior; may be passing, known-failing (control), or provisional pending a gated decision.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For a relevant query against a fixed graph, the reader returns 100% of the genuinely-relevant facts and 0% of the retrieved-but-irrelevant facts.
- **SC-002**: For a query with no genuinely-relevant fact, the reader returns zero facts (no irrelevant facts injected downstream) in 100% of negative-control cases.
- **SC-003**: The read-path relevant/irrelevant separation is preserved across at least one embedding-model change without altering a precise per-model separating value (only coarse/forgiving values may change).
- **SC-004**: Paraphrased duplicate notes are merged into exactly one surviving note, and the survivor's text is byte-identical to one of the original notes (zero rewrites) in 100% of paraphrase-merge cases.
- **SC-005**: Genuinely distinct notes are never merged (0% over-merge) across the distinct-ideas evaluation cases.
- **SC-006**: The two currently known-failing paraphrase-dedup evaluation cases pass, and the exact-match dedup case remains passing.
- **SC-007**: Each write embeds the incoming text exactly once, total — the merge decision, the contradiction decision, and the persisted fact all share that single vector (down from 2–3 embeddings of the same text per write today) — and a merged duplicate triggers zero contradiction checks.
- **SC-008**: Dedup and contradiction evaluations run deterministically offline (no live-model calls) by replaying committed verdict fixtures; a stale fixture is detected and surfaced 100% of the time rather than silently passing.
- **SC-009**: Explicit (negation-style) contradiction detection retains its prior pass rate, now sourced from structured output.
- **SC-010**: The implicit-contradiction experiment reports a measured tag co-assignment recall and end-to-end flag rate on its defined evaluation set, and the owner records a keep/kill decision based on those metrics (no pre-pinned threshold).
- **SC-011**: Every reconciled evaluation case asserts real shipped behavior; no case relies on a per-case-tuned constant to manufacture a pass.
- **SC-012**: The far-only recall-under-noise version returns 100% of relevant facts and 0% of distractors; the near-only version's outcome is recorded as provisional (pass or documented near-miss) with the boundary result captured, and the `reader_returns_all` `_before` control fails as expected under the new reader.
- **SC-013** *(Deferred — prerequisite-gated, with FR-030)*: The application eval suite (`matt/applications/*`) shows no unexplained regressions from the write-path changes; every application-case outcome that changes is accounted for by an intended behavior change with updated expectations. Measurable only once deterministic ingestion lands; not asserted by this feature's tasks.

## Assumptions

- **Scope is the eval-harness/offline knowledge-graph read and write paths.** Wiring the retrieving reader into the production serve path is explicitly a separate decision and is out of scope here; this feature only fixes the policies/contracts so evaluations are honest.
- **Three-stage rollout for the contradiction work.** Tier A (land dedup, unify dedup+conflict, harden judges, add the contradiction verdict fixture) is in scope. Tier B (the implicit-contradiction tag experiment) is in scope strictly as a gated experiment that may be dropped. Tier C (an offline/batch compaction backstop for residual missed contradictions) is documented as the honest fallback but is NOT built as part of this feature.
- **The verify stage is the existing LLM-style judge**, reusing the established in-write-policy precedent and its offline-skip behavior; a cross-encoder is a noted cost-driven fallback only and is not part of this feature.
- **Deterministic offline behavior follows the existing committed-fixture (cassette) pattern** already used for embeddings: model-keyed JSON, replay offline, record by key, loud miss on stale, skip when no credentials.
- **Decay/recency filtering is orthogonal** and unaffected by these recall policies.
- **Concrete numeric defaults** (existence floor, relative ratio, volume cap, recall floor) are calibrated against the committed retrieval/verdict data and documented alongside the model they were measured on; they are coarse/forgiving by design rather than precise separating lines, and recomputed on a model change.
- **The relative-fraction cutoff is the chosen read-path middle step** for the first cut (one predictable knob, no smooth-curve failure mode), with a gap-based variant left as a possible later refinement; the existence floor and volume cap stay regardless.
- **The exact unification surface of the merge and contradiction judges** (one combined step vs two sibling steps) is left flexible: they share candidate-finding and the recall floor, and the design must not foreclose a later single-judge surface, but full judge unification is not required here.

### Dependencies on recent ingestion changes (PR #14, commit `9478b69`)

- **Upstream ingestion now defers all reconciliation to the write path.** `PromptIngestor` splits raw input into verbatim atomic insights and explicitly does NOT dedupe, merge, or rewrite; dedup/conflict reconciliation happens solely in `graph.write` (`Deduper` → `ConflictFlagger`). This is exactly the step the write-path stories target, and it guarantees the merge-judge receives verbatim text — directly satisfying the verbatim-survivor requirement (FR-011) with no ingestion-time rewrite confound.
- **The write-policy changes have a wider blast radius than the dedicated dedup cases.** The application eval suite (`matt/applications/*`) now runs the real write policy end-to-end (`substrate: vector`, `embedder: live`, `ingest_model: gpt-4o-mini`). Changes to `Deduper`/`ConflictFlagger` from Stories 2–3 MUST therefore be validated against the application suite, not only `ingestion_merge_near_dupes`, `skills_merge_dedup`, and `ingestion_dedup`.
- **The new `ingest_model` EvalCase axis coexists with the reader axes this feature changes.** This feature replaces `reader_min_score` with `reader_abs_floor`/`reader_rel_ratio`; those additions sit alongside the recently-added `ingest_model` axis and must follow the same per-case override pattern. The dedicated dedup cases intentionally seed insights verbatim (`via_ingestor`, no `ingest_model`) so the write-policy merge — not ingestion distillation — is what is under test.

### Known gaps & risks

- **The application-suite validation (FR-030 / SC-013) runs against nondeterministic ingestion and uncached embeddings today.** Application cases use `ingest_model` (a live LLM splitter whose output text varies run-to-run, even at temperature 0) and `embedder: live` (a bare `OpenRouterEmbedder` with no cache wrapper — only `embedder: cached` is wired to the committed embedding fixture). Consequences: (1) the same incoming text is embedded 2–3× per write today (the smell FR-015/SC-007 fixes), and under `live` every one of those is a real API call; (2) the embedded text isn't stable, so a `(model, text)`-keyed embedding cache can't replay it — which is *why* these cases are on `live`, not an oversight. Deterministic ingestion (a text→text replay cassette over `ingest_model`, mirroring the embedding cache and this feature's merge/conflict verdict cassettes) would make `cached` embeddings viable for the application suite and cut replay embedding cost to zero — but it does NOT make those cases fully deterministic (they still run a live agent + judge). It is a separate eval-infrastructure concern, deliberately **out of scope** here and tracked in [`docs/proposals/2026-06-22-deterministic-ingestion-cassette.md`](../../docs/proposals/2026-06-22-deterministic-ingestion-cassette.md). FR-015's "embed once per write" is the in-scope mitigation that reduces the cost regardless of caching.
- **Nondeterministic ingestion makes the application suite unusable as a measurement instrument — so the cassette is a prerequisite, not merely a cost optimization.** Because each run re-distills the source with the live splitter, the set of facts in the graph changes run-to-run: an A/B probe (2026-06-22, whole-graph reader vs `reader: retrieving` on the three failing application cases) found that facts a check asserts (`billions`, `bentoml/mlops`) were present in the graph one run and absent the next, purely from re-ingestion. Consequence: a pass/fail swing on `matt/applications/*` cannot be attributed to any policy change (reader cutoff, dedup, or conflict) while ingestion drifts. Therefore FR-030/SC-013 are only meaningful **after** deterministic ingestion lands; the cassette is **sequenced as a gating prerequisite** for relying on application-suite validation. (The same probe did confirm the reader mechanically works — context shrank ~11k→~1k chars with no rubric loss, and one case flipped FAIL→PASS — but that signal is only reproducible once ingestion is fixed.)
- **A second prerequisite emerged from merging main's active-fact retrieval gating (PR #19): the application cases need their distilled facts marked retrievable.** With `search()`/`read()` gated to `active`, the application cases' `via_ingestor` facts land `proposed` and are hidden from the agent — which then reads an empty graph and writes ungrounded answers. They can't switch to `direct_to_graph` (that bypasses the very distiller they exist to exercise), so the fix is a case axis — e.g. `ingest_state: active` threaded into the ingestion seeding — that approves the distilled facts so they surface. This is gating-integration / eval-infra work, **out of scope** here; it is the **second prerequisite** (alongside the deterministic-ingestion cassette) for FR-030/SC-013 to be meaningful, and is owned by the gating author / a focused follow-up. (Our own component reader/dedup cases sidestep it: they use `direct_to_graph` (active) or inspect all stored states — see the producers in `knowledge/evals/run.py`.)
