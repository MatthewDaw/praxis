---
description: "Task list for Deterministic Ingestion Cassette"
---

# Tasks: Deterministic Ingestion Cassette

**Input**: Design documents from `specs/002-deterministic-ingestion-cassette/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: INCLUDED — the project follows TDD (write red tests first); the cassette's
record/replay/loud-miss/skip behavior is mechanism-isolation tested before wiring.

**Sequencing (not a task)**: this branch is **stacked on `001-us3-tier-b-implicit-contradiction`**,
so the reused 001 code (embed-once write path, `VerdictCassette` sibling, `CachedEmbedder`, the
`dom/` eval namespace used by T013) is already present — implementation can begin now. The 002 PR
**merges after** the 001 stack lands on `main`.

**Organization**: By user story (P1 → P2 → P3). US1 delivers deterministic ingestion (the MVP);
US2 flips the application suite to cached embeddings; US3 adds deterministic component cases.

## Path Conventions
Single Python package at repo root: `knowledge/...`. Tests live in per-package `tests/` dirs.

---

## Phase 1: Setup (Shared Infrastructure)

- [X] T001 [P] Create the committed-cassette fixture dir `knowledge/evals/fixtures/ingestion/` (add `.gitkeep`), mirroring `fixtures/embeddings/` and `fixtures/verdicts/`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Intentionally minimal.** US1 delivers the `IngestionCassette` itself; US2 and US3 build on it
(natural priority order). There are no blocking-all-stories code tasks beyond Setup — the 001-stack
precondition is already satisfied by stacking this branch on `001-us3-tier-b-implicit-contradiction`.

**Checkpoint**: Setup complete — US1 can begin.

---

## Phase 3: User Story 1 — Record-once / replay-offline ingestion (Priority: P1) 🎯 MVP

**Goal**: The ingestion splitter's `(ingest_model, raw input) → output text` is served from a
committed cassette: replayed offline with no live call, recorded on a miss with a key, loud on a
stale/uncached miss, skipped when no source. The graph an application case constructs is identical
across runs.

**Independent Test**: Run an `ingest_model` case twice offline against the committed cassette and
assert the set of distilled facts is byte-identical with zero live ingestion calls; change a seeded
input or the model id and confirm a loud miss.

### Tests for User Story 1 (write first, ensure they FAIL)

- [X] T002 [P] [US1] Cassette unit tests in `knowledge/tests/test_ingestion_cassette.py`: replay
  hit (no call), record-on-miss with a stub `inner`, loud-miss when recording disabled,
  skip-when-no-source, model-id-in-key (model swap → clean miss), record-then-replay round-trip
- [X] T003 [P] [US1] Harness skip test in `knowledge/evals/tests/test_run.py`: an `ingest_model`
  case is SKIPPED offline when neither a committed ingestion cassette nor a key is available
  (the `ingest_replay` capability gate)

### Implementation for User Story 1

- [X] T004 [US1] Implement `IngestionCassette` in `knowledge/llm/ingestion_cassette.py` — near-copy
  of `CachedEmbedder` with a `str → str` value codec: `sha256(model_id + "\n" + raw_input)` key,
  `allow_compute` gate, **loud `RuntimeError`** on a disabled miss, locked merge-on-save (parallel
  `--workers` safe); a `__call__(raw_input) -> str` shape
- [X] T005 [US1] Wire the cassette into `knowledge/evals/run.py` `_ingest_llm_for`: wrap the
  `str → str` ingest callable in `IngestionCassette` (parallel to `_eval_embedder`'s `cached`
  branch); `PromptIngestor` unchanged (still receives a `str → str` callable)
- [X] T006 [US1] Add the `ingest_replay` capability to `harness_capabilities` (committed ingestion
  cassette OR a key) + `case_needs` (`ingest_model` → `ingest_replay`) in `knowledge/evals/run.py`,
  so cassette-less offline cases SKIP rather than mis-run
- [X] T007 [US1] Implement the regenerator `knowledge/evals/ingestion_cache.py --refresh` (mirror
  `embed_cache.py`): require a key, delete the model's cassette, re-run every `ingest_model` case to
  record exactly the inputs they distill, persist for commit
- [X] T008 [US1] Record + commit the ingestion cassette
  `knowledge/evals/fixtures/ingestion/<model-slug>.json` for the `ingest_model` cases (live, with key)
- [X] T009 [US1] Verify SC-001/002/003 offline: an `ingest_model` case's ingestion runs twice →
  identical distilled facts, zero live ingestion calls; a changed input/model → loud miss

**Checkpoint**: Ingestion replays deterministically offline — the MVP is independently functional.

---

## Phase 4: User Story 2 — Cached embeddings for the application suite (Priority: P2)

**Goal**: With ingested text stable, every `matt/applications/*` case that uses `ingest_model`
runs on committed vectors (`cached`), so the graph-construction layer makes zero live calls.

**Independent Test**: After recording the ingestion cassette then refreshing the embedding cache,
run a previously-`live` application case offline on `cached` and confirm zero live ingestion or
embedding calls and a stable result.

**Depends on US1** (stable text is the precondition for a stable embedding key).

- [X] T010 [US2] Flip every `matt/applications/*` case that sets `ingest_model` from
  `embedder: live` to `embedder: cached` (`knowledge/evals/cases/matt/applications/**/case.yaml`)
- [X] T011 [US2] Refresh + commit the embedding cache for the now-stable distilled strings —
  **after** the ingestion cassette (FR-009 ordering): `embed_cache --refresh`, commit
  `knowledge/evals/fixtures/embeddings/*`
- [X] T012 [US2] Verify SC-004/005 offline: every flipped case runs on `cached` deterministically
  with zero live ingestion **or** embedding calls for graph construction (none remain on `live`)

**Checkpoint**: The application suite's graph-construction layer is deterministic + free on replay.

---

## Phase 5: User Story 3 — Deterministic component cases from real distilled insights (Priority: P3)

**Goal**: A dedup/conflict component case seeded from *real* cassetted LLM distillation (not
hand-written verbatim strings) replays deterministically offline.

**Independent Test**: Author a component case seeded via the cassetted distillation of a real input
and confirm it replays offline with identical facts every run.

**Depends on US1** (the cassette is the source of the deterministic real-distilled insights).

- [X] T013 [P] [US3] Author a deterministic component case (`component: ingestion` or
  `knowledge_graph`, `ingest_model` set, replayed from the committed cassette) under
  `knowledge/evals/cases/dom/` and confirm offline-identical facts (SC-006)

**Checkpoint**: Component cases can exercise the system on real distilled text, offline.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T014 [P] Document the two-step refresh ordering + loud-miss staleness ergonomics where the
  cassettes are described (quickstart already covers it; mirror into any fixtures/README)
- [X] T015 [P] Mark `docs/proposals/2026-06-22-deterministic-ingestion-cassette.md` Implemented and
  cross-link the spec; note the active-fact-retrievability follow-up (the 2nd FR-030/SC-013 prereq)
- [X] T016 Verify full offline determinism: committed ingestion + embedding fixtures replay with
  zero live calls; a stale fixture surfaces a loud miss; no live calls in CI

---

## Dependencies & Execution Order

### Phase Dependencies
- **Setup (Phase 1)**: no dependencies.
- **Foundational (Phase 2)**: minimal/none.
- **US1 (Phase 3)**: after Setup. Delivers the cassette + wiring + recording. **This is the MVP.**
- **US2 (Phase 4)**: **depends on US1** (stable text precedes stable embeddings; record ingestion → then embeddings).
- **US3 (Phase 5)**: **depends on US1** (needs the cassette as the source of real distilled insights). Independent of US2.
- **Polish (Phase 6)**: after the desired stories complete.

### Within each story
- Tests (red) before implementation; the cassette class before the wiring that uses it; the
  regenerator before recording real fixtures; cases flipped/recorded after the mechanism lands.

### Parallel opportunities
- Setup T001 alone.
- US1 tests T002 ∥ T003 (different files).
- US2 and US3 can proceed concurrently after US1 (different files: app-case YAMLs vs a new dom/ case).
- Polish T014 ∥ T015.

---

## Implementation Strategy

### MVP first (US1 only)
Setup → US1 → deterministic offline ingestion replay (record once, replay free, loud-miss). Ship
the determinism + cost win for the ingestion layer independently.

### Incremental delivery
US1 (deterministic ingestion) → US2 (cached embeddings for the app suite) → US3 (component cases on
real distilled text). Each is a deployable increment.

### Deferred (not in this feature)
- The unified keyed-replay abstraction across the four cassette surfaces (extract later).
- The **second** FR-030/SC-013 prerequisite — active-fact retrievability (`ingest_state: active`) —
  the explicit next follow-up, its own unit.
- Full application-case determinism (the live agent + judge stay nondeterministic).

---

## Notes
- `[P]` = different files, no incomplete-task dependency.
- Verify tests FAIL before implementing (TDD).
- The cassette mirrors `CachedEmbedder` exactly (committed, model-keyed, loud-miss, record-with-key,
  skip-when-unavailable) — reuse the shape, don't reinvent.
- Recording any fixture needs a live key locally; CI replays offline. Refresh order is always
  ingestion cassette first, embedding cache second.
- Commit after each task or logical group; keep each change traceable to a requirement.
