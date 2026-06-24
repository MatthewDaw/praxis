---
description: "Task list for REJECTED state + retained-contradiction lifecycle"
---

# Tasks: REJECTED state + retained-contradiction lifecycle

**Input**: Design documents from `/specs/003-fact-rejection-lifecycle/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/candidate-lifecycle-api.md](contracts/candidate-lifecycle-api.md), [quickstart.md](quickstart.md)

**Baseline**: Post-refactor (`dbf60d9`) single facts spine. No `candidates` table; server is Postgres-only; `/candidates` + `/contradictions` are already facts-backed via `FactsCandidates`. This feature **extends those routes** — no new `/facts…` API.

**Tests**: REQUIRED. Constitution Principle II (Test-First) is NON-NEGOTIABLE for behavior-changing code, and Principle I requires evals to defend write-path behavior changes. Every behavior change below lands red-first.

**Sequencing**: **All backend work completes before any frontend work** (matches the spec's "Backend first, dashboard second" assumption). The API contract is frozen at the end of Phase 5 before the frontend phase begins. Within each story, tasks still carry their `[USx]` label for traceability even where grouped by layer.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on incomplete tasks)
- **[Story]**: US1 / US2 / US3 (Setup/Foundational/Polish carry no story label)
- File paths are repo-relative.

---

## Phase 1: Setup

**Purpose**: Confirm the Postgres-backed test environment (the only supported path post-refactor).

- [X] T001 Confirm a Postgres 16 + pgvector instance is reachable (`PRAXIS_DB_URL` set) and bootstrap the schema: `uv run python -m knowledge.serve.db`, per [quickstart.md](quickstart.md). Backend lifecycle/server tests require it.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The `decayed`→`rejected` rename and the `contradicted_by` edge-kind write plumbing. Every user story depends on these.

**⚠️ CRITICAL**: No user-story work begins until this phase is complete.

### Tests (write first, must fail)

- [X] T002 [P] Write a failing migration read-back test (seed a fact with `state='decayed'`, run the rename migration, assert it reads back `rejected`) in `knowledge/serve/tests/test_reject_migration.py`.

### Rename `decayed` → `rejected` (FR-001, FR-002, SC-006)

- [X] T003 Change `FactState = Literal["proposed", "active", "rejected"]` (was `"decayed"`) and update the doc-comment block in `knowledge/knowledge_graph/knowledge_graph_def.py`.
- [X] T004 [P] Create the idempotent data migration `migrations/m2026_06_23_reject_rename.py`: `UPDATE facts SET state='rejected' WHERE state='decayed'` and the same for `cached_facts` (mirror the structure/guards of `migrations/m2026_06_23_unify_facts.py`).
- [X] T005 [P] Replace the `'decayed'` state strings with `'rejected'` in `knowledge/knowledge_graph/knowledge_graph_variants/vector_graph.py` (in-memory `fact.state = "decayed"`).
- [X] T006 [P] Replace the `'decayed'` state strings with `'rejected'` in `knowledge/serve/facts_candidates.py` (`reject`, `resolve`, `resolve_custom` — string only; behavioral changes come in later phases).
- [X] T007 [P] Update the `state` column comment in `knowledge/serve/schema.sql` and the retirement-state comment in `knowledge/knowledge_graph/write_policy/write_policy_def.py` (`decayed`→`rejected`).
- [X] T008 [P] Update the fixture state strings `decayed`→`rejected` in `knowledge/serve/data/candidates.json`.
- [X] T009 [P] Replace the `'decayed'` literal in the `_overwrite` SQL in `knowledge/knowledge_graph/knowledge_graph_variants/postgres_vector_graph.py` (string only here; the non-destructive rewrite is T016).
- [X] T010 [P] Update existing backend tests that assert `"decayed"` to `"rejected"` in `knowledge/serve/tests/test_server.py`, `knowledge/serve/tests/test_facts_candidates.py`, and `knowledge/knowledge_graph/tests/test_postgres_vector_graph.py` / `test_vector_graph.py`.
- [ ] T011 **(DEFERRED — handle deliberately, not in the rename pass)** Rename the eval cases `decayed_lesson_ignored` and `decayed_lesson_ignored_reader` → `rejected_lesson_ignored*` (directory names, `case.yaml` ids + embedded state strings, and the README) under `knowledge/evals/cases/`; keep them asserting real shipped behavior (Principle I), not a tuned constant. *Note: these cases seed insights as `active` (no runtime `state="decayed"`), so deferring does not break the suite; the rename entangles the `DECAYED_RIVAL_MARKER` and reader decay-filter naming, which is a separate concern.*

### Edge-kind write plumbing (FR-004, FR-007)

- [X] T012 Add a `contradicted_by` edge flip/upsert helper to `knowledge/knowledge_graph/knowledge_graph_variants/postgres_vector_graph.py` (flip the canonical pair row `kind='contradiction'`→`'contradicted_by'` idempotently; `add_edge`/`remove_edge` already exist). Used by both the `/insights` resolve path (US1) and the reviewer resolve path (US1/US2).

**Checkpoint**: `uv run pytest knowledge -q` is green except the still-unimplemented behavior tests; the term `decayed` is gone from backend code/state values.

---

## Phase 3: User Story 1 — Approving a correction preserves the fact it replaces (Priority: P1) 🎯 MVP

**Goal**: When an approved fact contradicts an existing one, keep both — the loser becomes `rejected` with its text intact, the pair is linked by a `contradicted_by` edge. No content is destroyed.

**Independent Test**: Approve a fact B that conflicts with live fact A; assert A still exists with original text, A is `rejected`, B is `active`, and a `contradicted_by` edge links them. (Backend-only; no frontend needed.)

### Tests for User Story 1 (write first, must fail)

- [X] T013 [P] [US1] Rewrite `knowledge/knowledge_graph/write_policy/tests/test_conflict_overwriter.py` to assert the loser's `text` is **unchanged**, its state is `rejected`, and a `contradicted_by` edge exists — replacing the old "text is overwritten" expectation (US1 #1, SC-001).
- [X] T014 [P] [US1] Add tests in `knowledge/knowledge_graph/tests/test_postgres_vector_graph.py` for non-destructive resolve over **several** conflicts and over a **proposed** (never-live) conflict: all become `rejected` + linked, none overwritten (US1 #2/#3, FR-006). Assert that only the direct conflicts of the approved fact change state — facts reachable only via a *separate* contradiction are untouched (FR-009, no auto-cascade).
- [X] T015 [P] [US1] Add a test in `knowledge/serve/tests/test_facts_candidates.py` that `resolve()` / `resolve_custom()` **flip the edge** (keep the link) rather than delete it, set the loser `rejected`, the winner `active`, and never modify text (FR-004).

### Implementation for User Story 1

- [X] T016 [US1] Rewrite `PostgresVectorGraph._overwrite` in `knowledge/knowledge_graph/knowledge_graph_variants/postgres_vector_graph.py`: the approved fact is a plain `add` at `state='active'`; **every** conflict (including the former `update_target_id`) is set `state='rejected'` with text/embedding untouched and gets a `contradicted_by` edge (via T012). Enforce the "never both active" invariant atomically (FR-003, FR-005, SC-001/SC-002).
- [X] T017 [US1] Update `ConflictOverwriter.apply` in `knowledge/knowledge_graph/write_policy/write_step_variants/conflict_overwriter.py` so all conflicts are treated uniformly as losers (no in-place text overwrite); reconcile `action`/`update_target_id`/`supersede_ids` semantics with T016.
- [X] T018 [US1] Change `FactsCandidates.resolve` and `resolve_custom` in `knowledge/serve/facts_candidates.py` to flip the edge kind (call the T012 helper) instead of `remove_edge`; set loser `rejected`, winner `active`; never touch text (FR-004, SC-003).
- [X] T019 [US1] Revisit the `/insights` read-back in `add_insight` (`knowledge/serve/app.py`) so the reported `action` stays sensible now that the approved fact is always a fresh `add` (report "added" with rejected-loser info).

**Checkpoint**: US1 is fully testable on the backend — approvals preserve contradicted facts and link them; the MVP increment is shippable (backend).

---

## Phase 4: User Story 2 (backend) — Review facts by state and resolve contradictions from a fact (Priority: P2)

**Goal (backend slice)**: The data + endpoints behind reviewing facts by state, seeing pending/resolved contradictions per fact, re-approving a rejected fact (swap winner), the global pending list, and the `hasOtherContradictions` signal. The UI for this lands in Phase 6.

**Independent Test (backend)**: Via the API: `GET /candidates/{id}` shows both pending and resolved contradictions with status; `GET /contradictions` lists only pending; promoting a rejected fact swaps states and keeps the link; reject/promote/resolve responses carry `hasOtherContradictions`.

### Tests for User Story 2 — backend (write first, must fail)

- [X] T020 [P] [US2] Test that `GET /candidates/{cid}` returns contradictions including **both** pending and resolved, each with the contradictor's `state` and `status` ∈ `pending|resolved`, in `knowledge/serve/tests/test_server.py` (FR-012).
- [X] T021 [US2] Test that `GET /contradictions` lists only pending pairs (`kind='contradiction'`) and that a resolved pair drops out of it while staying visible per-fact, in `knowledge/serve/tests/test_server.py` (FR-013a, US2 #6). *(Same file as T020 — sequence after it.)*
- [X] T022 [P] [US2] Test re-approval: promoting a `rejected` fact flips it to `active`, demotes its active contradictor to `rejected`, and keeps the `contradicted_by` link, in `knowledge/serve/tests/test_facts_candidates.py` (FR-010, SC-003). **Also assert no auto-cascade (FR-009)**: a fact linked to the re-approved fact only through a *separate* contradiction is **not** touched — only the direct contradictor changes state.
- [X] T023 [US2] Test `hasOtherContradictions`: a rejected fact with another contradiction reports `true`; the just-resolved pair alone reports `false`, in `knowledge/serve/tests/test_facts_candidates.py` (FR-008, SC-007). *(Same file as T022 — sequence after it.)*

### Implementation for User Story 2 — backend

- [X] T024 [US2] Update `_rival_map` in `knowledge/serve/facts_candidates.py` to read **both** `contradiction` and `contradicted_by` edges, and surface per-rival `state` + `status` through `fact_to_candidate` in `knowledge/serve/pipeline_adapter.py` and `serialize_pairs` in `knowledge/serve/contradiction_adapter.py` (FR-012).
- [X] T025 [US2] Compute `hasOtherContradictions` in `knowledge/serve/facts_candidates.py` (edge touching the fact other than the just-resolved A↔B row) and include it in the `reject` / `promote` / `resolve` responses (FR-008).
- [X] T026 [US2] Extend `FactsCandidates.promote` (`knowledge/serve/facts_candidates.py`) to allow `rejected → active` re-approval: demote the currently-active contradictor(s) to `rejected`, keep the `contradicted_by` edge (FR-010).
- [X] T027 [US2] In `knowledge/serve/app.py`, filter `GET /contradictions` to pending (`kind='contradiction'`) and ensure the `resolve` / `reject` / `promote` route responses carry `hasOtherContradictions` (FR-013a, FR-008).

**Checkpoint**: US1 + US2 backend behavior complete and tested via the API.

---

## Phase 5: User Story 3 (backend) — Delete facts safely, with live facts protected (Priority: P3)

**Goal (backend slice)**: Permit deleting only `proposed`/`rejected` facts; refuse `active` with a 409 directing the user to reject first; deleting removes the fact's contradiction links. The UI 409 handling lands in Phase 6.

**Independent Test (backend)**: `DELETE` an active fact → 409; `proposed`/`rejected` → 200 with `fact_edges` gone and the fact absent from other facts' contradiction lists.

### Tests for User Story 3 — backend (write first, must fail)

- [X] T028 [P] [US3] Test `DELETE /candidates/{id}`: `active` → 409 with reject-first guidance; `proposed`/`rejected` → 200 and `fact_edges` gone; the fact disappears from other facts' contradiction lists, in `knowledge/serve/tests/test_server.py` (US3 #1–#3, SC-005).
- [X] T029 [P] [US3] Test `FactsCandidates.delete` state gating (raises a precondition error on `active`, deletes otherwise) in `knowledge/serve/tests/test_facts_candidates.py` (FR-014, FR-015).

### Implementation for User Story 3 — backend

- [X] T030 [US3] Add state gating to `FactsCandidates.delete` in `knowledge/serve/facts_candidates.py`: raise a precondition error when `state == 'active'`; delete (edges cascade) when `proposed`/`rejected` (FR-014, FR-016).
- [X] T031 [US3] Map the precondition error to **HTTP 409** with `{ detail: "reject the fact before deleting" }` in the `delete_candidate` route in `knowledge/serve/app.py` (FR-014).

**Checkpoint**: 🔒 **API contract frozen.** All backend behavior (US1–US3) is implemented and tested; the renamed state value, `status`, and `hasOtherContradictions` fields are stable. Frontend work can now begin against a fixed contract.

---

## Phase 6: Frontend (User Stories 2 & 3)

**Purpose**: All dashboard work, against the now-frozen API contract. Backend has no dependency on anything here.

### Tests (write first, must fail)

- [ ] T032 [P] [US2] Update the frontend contract/fixture tests for the renamed state value and the new response fields (`status`, `hasOtherContradictions`) in `frontend-react/src/api/contract.test.ts` and `frontend-react/src/api/contractFixtures.test.ts` (Principle III).

### Implementation — rename (FR-001, SC-006)

- [X] T033 [US2] Rename `decayed`→`rejected` (state value, class/label maps) in `frontend-react/src/types/candidate.ts`, `frontend-react/src/api/candidateModel.ts`, `frontend-react/src/components/StateBadge.tsx`, `frontend-react/src/components/layout/FilterBar.tsx`, `frontend-react/src/components/viz/legendConfig.ts`, `frontend-react/src/api/mockProvider.ts`, and `frontend-react/src/api/localLogsProvider.ts`.
- [X] T034 [P] [US2] Update affected frontend tests for the rename (`frontend-react/src/components/viz/legendConfig.test.ts` and any provider/model tests asserting `decayed`).

### Implementation — contradiction review UX

- [ ] T035 [P] [US2] Update `frontend-react/src/components/ContradictionPanel.tsx` to show each contradictor with its `state` + pending/resolved `status` and exactly one state-gated action: "Reject" for active/proposed, "Approve" for rejected (FR-013).
- [ ] T036 [P] [US2] Update `frontend-react/src/components/ContradictionsReview.tsx` to render the global pending-contradictions view from `GET /contradictions` (FR-013a).
- [ ] T037 [US2] Show the review notice when an action returns `hasOtherContradictions: true` (link to the affected fact), and refresh affected facts after each action without a manual reload (FR-008, FR-017, SC-007).
- [ ] T038 [P] [US3] Handle the 409 on delete-of-active using the existing `ApiConflictError { statusCode: 409 }` and show "reject the fact first" guidance (the delete call site / its colocated test in `frontend-react/src/api/`).

**Checkpoint**: All three user stories are functional end-to-end.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T039 [P] Run the backend gate green: `uv run pytest knowledge -q` (including the renamed `rejected_lesson_ignored*` eval cases).
- [ ] T040 [P] Run the frontend gates green: `cd frontend-react && npm test && npm run lint && npm run build`.
- [ ] T041 [P] Verify SC-006: grep `frontend-react/src` and `knowledge` for `decayed` and confirm only intentional historical references remain (no user-facing labels/filters/state values).
- [ ] T042 Confirm the contract↔fixtures↔clients sync (Principle III): `contracts/candidate-lifecycle-api.md` matches the Python routes and the React client for the renamed state value and the `status` / `hasOtherContradictions` fields.
- [ ] T043 Run the [quickstart.md](quickstart.md) manual walk-through of User Story 1–3 acceptance scenarios against Postgres.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)**: no dependencies.
- **Foundational (Phase 2)**: depends on Setup. **BLOCKS everything** (rename + edge plumbing are shared).
- **US1 backend (Phase 3, P1)**: depends on Foundational. The MVP.
- **US2 backend (Phase 4, P2)**: depends on Foundational; builds on US1's resolved-edge writes for its read/display.
- **US3 backend (Phase 5, P3)**: depends on Foundational; independent of US1/US2 behavior.
- **Frontend (Phase 6)**: depends on the **frozen API contract** at the end of Phase 5. No backend task depends on it.
- **Polish (Phase 7)**: depends on all targeted work.

### Within each story

- Tests are written first and must fail before implementation (Principle II).
- US1: T013–T015 (tests) → T016/T017 (graph + policy) → T018 (facade) → T019 (read-back).
- US2 backend: T020–T023 (tests) → T024–T027 (impl).
- US3 backend: T028/T029 (tests) → T030 → T031.
- Frontend: T032 (contract test) → T033 (rename) → T034 (rename tests) → T035–T037 (UX) → T038 (409).

### Same-file sequencing (not parallel)

- `postgres_vector_graph.py`: T009 (string) → T012 (edge helper) → T016 (non-destructive `_overwrite`).
- `facts_candidates.py`: T006 → T018 → T024 → T025 → T026 → T030.
- `app.py`: T019 → T027 → T031.
- `test_server.py`: T020 → T021 → T028.
- `test_facts_candidates.py`: T015 → T022 → T023 → T029.

## Parallel Opportunities

- **Foundational rename**: T004, T005, T007, T008 touch different files → parallel (run T003 first as the type source; keep T006/T009/T010 in their within-file order).
- **US1 tests**: T013, T014, T015 → parallel (different test files).
- **US2 backend tests**: T020 ∥ T022 (different files); T021 follows T020, T023 follows T022.
- **US3 backend tests**: T028, T029 → parallel (different files).
- **Frontend**: T035 ∥ T036 ∥ T038 (different files); T037 after T035/T036.
- **Polish**: T039, T040, T041 → parallel.

### Parallel example: User Story 1 tests

```bash
Task: "Rewrite test_conflict_overwriter.py — loser text intact + rejected + contradicted_by edge"
Task: "Add multi-conflict + proposed-conflict tests in test_postgres_vector_graph.py"
Task: "Add resolve()/resolve_custom() edge-flip test in test_facts_candidates.py"
```

## Implementation Strategy

### Backend-first (this ordering)

Complete Phases 1–5 (all backend, US1→US2→US3) before any frontend. The API contract is frozen at the Phase 5 checkpoint, so the frontend phase builds against a stable surface and the work stays in one language at a time. Trade-off vs. a vertical-slice order: US2/US3 are not clickable end-to-end until Phase 6 — there is no per-story UI demo before then.

### MVP

Phases 1–3 (Setup + Foundational + US1 backend) deliver the core, highest-value change: approvals preserve contradicted facts and link them, text never destroyed.

## Notes

- [P] = different files, no dependency on incomplete tasks.
- Backend lifecycle/server tests require Postgres (no offline path post-refactor); inject `FakeEmbedder` / no-LLM policy for determinism (Principle II).
- The biggest behavioral risk is the **two** destructive paths (`_overwrite` and `resolve`'s `remove_edge`); T016 and T018 must both land for FR-003/FR-004 to hold.
- Commit after each task or logical group; conventional-commit style.
