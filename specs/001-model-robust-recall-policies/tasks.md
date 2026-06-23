---
description: "Task list for Model-Robust Recall Policies"
---

# Tasks: Model-Robust Recall Policies for the Knowledge Graph

**Input**: Design documents from `specs/001-model-robust-recall-policies/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: INCLUDED — FR-027 mandates mechanism-isolation + integration tests, and the project follows TDD (write red tests first).

**Organization**: By user story (P1 → P2 → P3), matching the confirmed sequencing **US1 (independent) → US2 (+ shared cassette infra) → US3 (depends on US2) → ingestion-cassette follow-on (separate spec)**.

## Path Conventions
Single Python package at repo root: `knowledge/...`. Tests live in per-package `tests/` dirs.

---

## Phase 1: Setup (Shared Infrastructure)

- [ ] T001 [P] Create verdict fixture dirs `knowledge/evals/fixtures/verdicts/merge/` and `knowledge/evals/fixtures/verdicts/conflict/` (add `.gitkeep`)
- [ ] T002 [P] Ensure test dirs exist: `knowledge/graph_reader/tests/`, `knowledge/knowledge_graph/write_policy/tests/`, `knowledge/llm/tests/` (create with `__init__.py` if missing)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Intentionally minimal.** US1 (reader) is fully standalone and shares nothing with the write-path work; the shared verdict-cassette infrastructure is delivered at the *start* of US2 (T015–T016) per the agreed sequencing, and US3 builds on US2. There are therefore no blocking-all-stories tasks beyond Setup.

**Checkpoint**: Setup complete — US1 can begin immediately.

---

## Phase 3: User Story 1 — Model-robust reader cutoff (Priority: P1) 🎯 MVP

**Goal**: `RetrievingReader` applies floor → relative → cap, drops irrelevant-present facts, returns nothing on no-match, and survives an embedding-model swap without re-tuning a precise value.

**Independent Test**: Run the reader over a fixed graph with committed vectors: all relevant facts kept, distractors dropped, no-match → empty — no live calls. Fully independent of the write-path stories.

### Tests for User Story 1 (write first, ensure they FAIL)

- [X] T003 [P] [US1] Isolation test *relative-drop* (`abs_floor=0`): the relative cutoff alone drops CloudFront/X-Ray/SES in `knowledge/graph_reader/tests/test_retrieving_reader.py`
- [X] T004 [P] [US1] Isolation test *relative-keep-all* (`abs_floor=0`): all N varying-strength relevant facts survive, in `knowledge/graph_reader/tests/test_retrieving_reader.py`
- [X] T005 [P] [US1] Isolation test *floor-empties* (`rel_ratio=0`): a no-relevant-fact query returns empty, in `knowledge/graph_reader/tests/test_retrieving_reader.py`
- [X] T006 [P] [US1] Integration test: production defaults (floor+relative+cap) keep relevant / drop irrelevant end-to-end, in `knowledge/graph_reader/tests/test_retrieving_reader.py`
- [X] T007 [P] [US1] Model-robustness test: relevant/irrelevant split holds under a second (scaled) embedder without changing a precise separating value, in `knowledge/graph_reader/tests/test_retrieving_reader.py` (also added a cap/volume-backstop test)

### Implementation for User Story 1

- [X] T008 [US1] Implement floor → relative → cap in `knowledge/graph_reader/grapher_reader_variants/retrieving_reader.py` (`__init__(graph, *, top_k=8, abs_floor=0.30, rel_ratio=0.75)`; remove `min_score`)
- [X] T009 [US1] Update `EvalCase` in `knowledge/evals/eval_def.py`: add `reader_abs_floor` / `reader_rel_ratio`, remove `reader_min_score` (subsumed)
- [X] T010 [US1] Thread `reader_abs_floor` / `reader_rel_ratio` through `knowledge/wiring.py` (`build_trio`)
- [X] T011 [US1] Thread the new axes through `knowledge/evals/run.py` (`_build_trio_for`)
- [X] T012 [P] [US1] Reconcile `lost_in_middle_reader`: set `reader_abs_floor: 0`, drop `reader_min_score`, remove the PROVISIONAL note (`knowledge/evals/cases/lost_in_middle_reader/case.yaml`) — passes offline 4/4
- [X] T012a [US1] Update existing `knowledge/tests/test_graph_reader.py` to the new abs_floor/rel_ratio API (required by the `min_score` removal)
- [X] T013 [P] [US1] Convert `reader_returns_all` → `reader_returns_all_before` (XFAIL control: retrieving reader keeps only the config fact, dump-all assertion fails); `after` omitted as redundant with `lost_in_middle_reader` (noted in-case). Replays offline XFAIL.
- [X] T014 [P] [US1] Redesign `scattered_multifact` → recall-under-noise reader test (far-only PASS 5/5: 3 conventions cluster 0.54-0.59, far distractors ≤0.15) + `scattered_multifact_near` (near-only **provisional**, XFAIL 4/5: same-topic distractor survives — documented boundary).
- [X] T015 [P] [US1] Convert no-leak cases to floor tests: `negative_control_irrelevant` (PASS — floor empties the CSS graph for a Python query) and `context_budget_overload` (PASS — ZEBRA_RULE stays below the floor at ~50-fact scale).
- [X] T016a [US1] Recalibrate global `rel_ratio` 0.75 → 0.60: the two reader cases pin it to (0.527, ~0.6]; 0.60 drops `lost_in_middle`'s CloudFront (0.272) with headroom and keeps `scattered_multifact`'s weakest convention. Reader default + docstring updated; integration test uses the default.
- [X] T016 [US1] Calibrate `abs_floor`/`rel_ratio`/`top_k` against the committed `text-embedding-3-small` cache; document the model + values on `RetrievingReader` (values validated by the lost_in_middle pair; documented on the reader)

**Checkpoint**: US1 fully functional and independently testable (the read-path MVP).

---

## Phase 4: User Story 2 — Semantic dedup of paraphrases (Priority: P2)

**Goal**: Paraphrased notes merge into one **verbatim** survivor via a loose recall gate + LLM `MergeJudge`, replayed offline from a committed merge-verdict cassette; distinct ideas preserved.

**Independent Test**: Ingest a paraphrase pair → one merged survivor verbatim with bumped count; ingest a distinct pair → both kept — replayed from the merge cassette, no live calls.

### Shared cassette infrastructure (delivered here, reused by US3)

- [X] T017 [US2] Implement `VerdictCassette` in `knowledge/llm/verdict_cassette.py` (keyed `sha256(model+payload)→verdict`, replay/record/loud-miss, merge-on-save under a process lock)
- [X] T018 [P] [US2] Cassette tests (5): replay hit, record-on-miss, loud-miss-when-disabled, model-id-in-key, concurrent-save merge, in `knowledge/tests/test_verdict_cassette.py`

### Tests for User Story 2 (write first, ensure they FAIL)

- [X] T019 [P] [US2] `MergeJudge` tests (5): same_lesson true/false, skip-when-no-source, cassette replay, verbatim-survivor, in `knowledge/knowledge_graph/write_policy/tests/test_merge_judge.py`
- [X] T020 [P] [US2] `Deduper` tests (added to `knowledge/tests/test_write_policy.py`): exact short-circuit unchanged, semantic-merge-on-yes, no-merge-on-no, below-floor skips the judge, no-judge=exact-only

### Implementation for User Story 2

- [X] T021 [US2] Implement `MergeJudge` in `knowledge/knowledge_graph/write_policy/write_step_variants/merge_judge.py` (yes/no same-lesson over `OpenRouterLlm`, backed by `VerdictCassette`; existing note is the verbatim survivor; skip when no source)
- [X] T022 [US2] Update `Deduper`: `threshold`→`recall_floor`; exact short-circuit kept; recall-gate → `MergeJudge`; no-judge = exact-only (backward compatible — all existing `Deduper()` callers still work)
- [X] T023 [US2] Wire the merge judge/cassette into `knowledge/evals/run.py` via a `merge_model` `EvalCase` axis + a `merge_verdicts` capability (graceful SKIP when no key/cassette); `_merge_judge_for` builds the judge, injected into `Deduper`.
- [X] T024 [US2] Add `knowledge/evals/verdict_cache.py` regenerator (mirrors `embed_cache.py`, `--refresh`; records merge verdicts + any embedding misses).
- [X] T025 [US2] Generated + committed the merge verdict cassette `knowledge/evals/fixtures/verdicts/merge/openai_gpt-4o-mini.json` (+ embedding vectors for the two cases).
- [X] T026 [P] [US2] Flip `ingestion_merge_near_dupes`: `embedder: cached` + `merge_model`; xfail dropped → **PASS 1/1** offline (paraphrases merge).
- [X] T027 [P] [US2] Flip `skills_merge_dedup`: `embedder: cached` + `merge_model`; xfail dropped → **PASS 2/2** offline (shared idea merged, distinct ideas survive).

**Checkpoint**: US2 works independently; the two paraphrase-dedup cases pass against cassetted verdicts; `ingestion_dedup` (exact) still passes.

---

## Phase 5: User Story 3 — Unified, hardened, recall-aware contradiction (Priority: P3)

**Goal**: One candidate-recall pass per write (embed once), shared by merge + conflict; `ConflictFlagger` emits structured output replayed from a conflict cassette; merge-before-conflict; plus a **gated** Tier-B implicit-contradiction experiment and a documented Tier-C residual.

**Independent Test**: Assert exactly one embedding of the incoming text per write; one recall pass feeds both judges; a merged dup skips the conflict check; a negation-contradiction pair flags via structured output from the cassette.

**Depends on US2** (reuses `VerdictCassette` + the recall-gate shape).

### Tests for User Story 3 (write first, ensure they FAIL)

- [X] T028 [P] [US3] Embed-once test: a single write embeds the incoming text exactly once (merge + conflict + persist share the vector), in `knowledge/knowledge_graph/tests/test_vector_graph.py`
- [X] T029 [P] [US3] Shared-recall test: one `most_similar` pass feeds both judges; a merged dup (`action==update`) triggers zero conflict checks, in `knowledge/knowledge_graph/tests/test_vector_graph.py` (+ below-floor candidate skipped by the recall gate)
- [X] T030 [P] [US3] `ConflictFlagger` structured-output test (stub judge `{contradicts}`, runtime `target_id`); cassette replay/loud-miss, in `knowledge/knowledge_graph/write_policy/tests/test_conflict_flagger.py`

### Implementation for User Story 3 — Tier A

- [X] T031 [US3] Make `knowledge/knowledge_graph/knowledge_graph_variants/vector_graph.py` (+ `postgres_vector_graph.py`) vector-aware: embed once onto `WriteDecision.embedding`; one shared recall pass (`_recall`) + single `recall_floor`; reuse the vector in `_add`/`_overwrite` (no store-time re-embed); merge-before-conflict, skip-conflict-on-update. Steps now consume `decision.candidates` (dropped `store`/`StoreView`); `consumes_candidates` flag triggers the shared pass.
- [X] T032 [US3] Update `WriteDecision` in `knowledge/knowledge_graph/write_policy/write_policy_def.py`: add `embedding` + `candidates` fields (the shared per-write recall, consumed by both judges)
- [X] T033 [US3] Structured conflict judge: new `ConflictJudge` (mirror of `MergeJudge`) emits `{contradicts}` over the `Llm` seam, backed by a conflict `VerdictCassette` (replay/loud-miss/graceful-skip); `ConflictFlagger` consumes shared candidates + injected judge and resolves `target_id` to the candidate's runtime id (replaces `startswith("yes")`). `default_write_policy` builds `ConflictFlagger(judge=ConflictJudge(llm=...))`.
- [X] T034 [US3] Wire the conflict cassette into `knowledge/evals/run.py` (`conflict_model` axis on `EvalCase`, `_conflict_judge_for`, `conflict_verdicts` capability in `harness_capabilities`/`case_needs`, `ConflictFlagger` into `_build_trio_for`, `knowledge_graph` producer surfaces `graph.contradictions()`); `verdict_cache --refresh` records conflict cases; generated + committed `knowledge/evals/fixtures/verdicts/conflict/openai_gpt-4o-mini.json` (+ embeddings)
- [X] T035 [P] [US3] Added the structured-output, offline-deterministic negation-contradiction component case `conflict_should_flag` (knowledge_graph/vector/cached + `conflict_model`): asserts the ConflictFlagger flags the contradiction via replayed `{contradicts}` verdict — PASS 2/2 offline. (The full-pipeline `contradiction_should_flag`/`scoped_conflict` cases grade the live agent's prose, a different layer, and are left as-is.)

### Implementation for User Story 3 — Tier B (gated experiment)

- [X] T036 [US3] `AspectTagger` + `AspectJudge` in `aspect_tagger.py` (controlled `ASPECT_VOCAB`, structured `{tags}`, cassette-replayed, graceful skip); `Fact.tags` field. Write-time tags on the incoming note.
- [X] T037 [US3] Union `same-tag` candidates into the **conflict** recall path only, bounded by `tag_recall_k` (`vector_graph._recall` builds `decision.tag_candidates`; `conflict_flagger` unions cosine ∪ same-tag, deduped — the Deduper still sees cosine only).
- [X] T038 [P] [US3] Built the implicit-contradiction eval set (`implicit_conflict_*`, 8 disjoint-vocab/no-negation pairs, all below the 0.45 floor) + `tag_model` axis / `_aspect_tagger_for` / `tag_verdicts` capability wiring; committed aspect+conflict cassettes + embeddings.
- [X] T039 [US3] `tier_b_metrics.py` reports co-assignment + end-to-end flag + cosine-only baseline + rescued-by-tags; strengthened the set to genuine below-floor pairs (first set skewed above-floor); surfaced to owner. Decision recorded in spec SC-010 + research R8.
- [X] T040 [US3] Gate outcome — **KEEP** (owner, 2026-06-23): promoted the 7 rescued cases to PASS, kept `documentation_policy` as a documented XFAIL residual (FR-023); tag mechanism kept opt-in (not in production `default_write_policy`).

**Checkpoint**: US3 Tier A hardens + unifies the write path; Tier B is measured and decided; Tier C (batch backstop) remains documented-only per spec FR-023.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T041 [P] Ran `quickstart.md` validation offline (`--fake`; the doc's `--structured` is a live backend, so `--fake` is the truly-offline runner for the deterministic component flips): dedup XFAIL→PASS, reader cluster reconciled (`lost_in_middle_reader` PASS, `reader_returns_all_before`/`scattered_multifact_near` XFAIL), conflict PASS, implicit 7 PASS + 1 XFAIL — zero live calls. Mechanism-isolation pytest subsets (graph_reader/knowledge_graph/llm) green.
- [X] T042 [P] Marked the three source proposals Implemented (`reader-cutoff-policy`, `semantic-dedup-recall-gate-llm-judge`, `unified-dedup-conflict-recall`) with the spec/Tier cross-references; cross-linked the deterministic-ingestion cassette follow-on. (De-linked a few pre-existing rotted/bot-blocked external citations the link-checker flagged.)
- [X] T043 Verified full-suite offline determinism: committed cassettes (merge/conflict/aspect) + embeddings replay with zero live calls (`OPENROUTER_API_KEY=`); stale/uncached fixture surfaces a loud `RuntimeError` (covered by `test_verdict_cassette` + conflict/aspect loud-miss tests).
- [ ] T044 Follow-on (separate spec, NOT this feature): deterministic-ingestion cassette is required before relying on the application suite for FR-030/SC-013 — link `docs/proposals/2026-06-22-deterministic-ingestion-cassette.md`

---

## Dependencies & Execution Order

### Phase Dependencies
- **Setup (Phase 1)**: no dependencies.
- **Foundational (Phase 2)**: minimal/none — does not block US1.
- **US1 (Phase 3)**: after Setup. Fully independent (read path only). **This is the MVP.**
- **US2 (Phase 4)**: after Setup. Independent of US1. Delivers the shared `VerdictCassette`.
- **US3 (Phase 5)**: **depends on US2** (reuses `VerdictCassette` + recall-gate shape).
- **Polish (Phase 6)**: after the desired stories complete.

### Story independence
- US1 ⟂ US2 (no shared files; can run in parallel by different people).
- US3 → US2 (sequential).

### Within each story
- Tests (red) before implementation; data/model changes before the steps that consume them; cases reconciled after the mechanism lands.

### Parallel opportunities
- Setup: T001, T002.
- US1 tests T003–T007 in parallel; case reconciliations T012–T015 in parallel (different files).
- US2 cassette test T018 ∥ judge/deduper tests T019–T020; case flips T026–T027 in parallel.
- US3 tests T028–T030 in parallel; T035/T038 in parallel.
- US1 and US2 can proceed concurrently after Setup.

---

## Parallel Example: User Story 1

```bash
# Reader tests (write first, all in one file but independent cases — or split):
Task: "Isolation relative-drop / relative-keep-all / floor-empties / integration / model-robustness"
# Case reconciliations (different files, parallel):
Task: "lost_in_middle_reader axes" ; "reader_returns_all → _before" ; "scattered_multifact two versions" ; "no-leak floor tests"
```

---

## Implementation Strategy

### MVP first (US1 only)
Setup → US1 → validate the reader cutoff on component cases → ship the read-path improvement independently.

### Incremental delivery
US1 (read path) → US2 (dedup + cassette infra) → US3 (unify + conflict + gated Tier B). Each is a deployable increment; US3 follows US2.

### Deferred (not in this feature)
Deterministic-ingestion cassette (its own spec) before application-suite validation (FR-030/SC-013); wiring `RetrievingReader` into the serve path.

---

## Notes
- `[P]` = different files, no incomplete-task dependency.
- Verify tests FAIL before implementing (TDD; FR-027).
- Numeric defaults are coarse/model-documented, calibrated against the committed cache — never per-case-tuned (FR-005/FR-024).
- Commit after each task or logical group; keep each change traceable to a requirement.
