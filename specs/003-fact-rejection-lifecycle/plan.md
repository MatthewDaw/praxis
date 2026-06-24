# Implementation Plan: REJECTED state + retained-contradiction lifecycle

**Branch**: `003-fact-rejection-lifecycle` | **Date**: 2026-06-23 (re-planned post-refactor) | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/003-fact-rejection-lifecycle/spec.md`

> **Re-plan note**: This plan was regenerated after commit `dbf60d9` ("collapse knowledge
> graph onto a single facts spine") landed. The original plan targeted a separate candidate
> store, treated `fact_edges` as unused, and proposed a parallel `/facts…` API. None of that
> holds anymore. See [research.md](research.md) for the reconciled decisions.

## Summary

Rename the fact retirement state `decayed` → `rejected`, and stop destroying the losing
fact's text when an approved fact contradicts an existing one. Today two paths destroy or
unlink knowledge: `PostgresVectorGraph._overwrite` (used by `/insights` via
`ConflictOverwriter`) rewrites the nearest conflict's `text` in place, and
`FactsCandidates.resolve` **deletes** the contradiction edge when a reviewer picks a winner —
losing the link the spec wants kept. This feature makes resolution **non-destructive and
linked**: keep both facts, mark the loser `rejected`, and record the relationship as a
*resolved* edge (`kind='contradicted_by'`) instead of dropping it. Deletion is gated to
`proposed`/`rejected` facts (409 on `active`). The work **extends the existing, already
facts-backed `/candidates` and `/contradictions` routes** — no parallel `/facts…` API.

## Technical Context

**Language/Version**: Python ≥3.12 (backend, `knowledge/`); TypeScript + React 18 (Vite) for `frontend-react/`

**Primary Dependencies**: FastAPI, psycopg 3 (autocommit), pgvector, pydantic v2 (backend); React + Vite + Vitest (frontend)

**Storage**: PostgreSQL 16 + pgvector. **Four tables only** (post-refactor): `facts`, `fact_edges` (live) and `cached_facts`, `cached_fact_edges` (saved states keyed by `cache_key`). The `candidates` table was dropped. Schema is the single idempotent `schema.sql` applied by `db.py :: bootstrap()`; one-off data migrations live in `migrations/` (e.g. `m2026_06_23_unify_facts.py`).

**Testing**: pytest (backend, under `knowledge/**/tests`, 54 passing); Vitest (frontend, colocated `*.test.ts(x)`, 77 passing)

**Target Platform**: Linux server (App Runner / Render) + static React dashboard. **Server is Postgres-only** — `create_app()` opens one shared autocommit connection and *requires a resolvable DSN* ([app.py:76-82](../../knowledge/serve/app.py)). There is no JSON/offline store anymore.

**Project Type**: Web application (Python backend + React frontend), already established in-repo

**Performance Goals**: Not performance-sensitive — human-paced review actions on a per-`(org,user)` graph.

**Constraints**: Multi-tenant isolation `(org_id, user_id)` on every row and edge; the "two contradicting facts never both `active`" invariant must hold under concurrent resolutions (enforce atomically at write time — last writer wins). No production data to preserve, so the rename is a one-shot idempotent data update.

**Scale/Scope**: Small per-tenant graphs (tens–hundreds of facts). The change touches the conflict-resolution write path, `FactsCandidates`, the existing candidate/contradiction routes, the `decayed`→`rejected` rename across backend + frontend + evals, and the contradiction-review UX.

## Constitution Check

*GATE: evaluated against `.specify/memory/constitution.md` v1.0.0 (now ratified — it was an empty template when this feature was first planned).*

- **I. Evals Are the Credibility Layer** — PASS with obligation. The conflict-resolution write path is behavior-changing. The `decayed_lesson_ignored` / `decayed_lesson_ignored_reader` eval cases ([knowledge/evals/cases/](../../knowledge/evals/cases/)) already cover the retirement-state behavior; they MUST be renamed to `rejected_*` and continue to assert real shipped behavior (Principle I forbids per-case-tuned constants). The retention/non-destructive change is additionally guarded by backend tests (below).
- **II. Test-First Quality Gates (NON-NEGOTIABLE)** — PASS (planned). Every behavior change lands red-first: a regression test asserting the loser's `text` is preserved (replacing the current overwrite assertion), edge-flip on resolve, delete-gating 409, re-approval state-swap, `hasOtherContradictions`, and a migration read-back test. `uv run pytest knowledge` and `npm test`/`lint`/`build` are the merge gates.
- **III. Contract-Driven Boundaries** — PASS. API shape changes (state-value rename, delete 409, `hasOtherContradictions`, pending/resolved status) update the contract doc ([contracts/candidate-lifecycle-api.md](contracts/candidate-lifecycle-api.md)), the canonical fixtures, and **both** the Python routes and the React client together.
- **IV. Offline-First & Graceful Degradation** — **DEVIATION (pre-existing, not introduced here)**. The principle mandates a JSON/in-memory fallback when Postgres is absent. Commit `dbf60d9` made the server Postgres-only and deleted the JSON candidate store *before* this feature. This plan inherits that constraint and does **not** re-add an offline path (doing so is out of scope and would balloon the change). Tracked as an explicit constitution-amendment follow-up: [docs/proposals/2026-06-23-principle-iv-offline-first-amendment.md](../../docs/proposals/2026-06-23-principle-iv-offline-first-amendment.md) — not silently accepted.
- **V. Provenance, Human Gating & Tenant Isolation** — PASS (strengthened). The feature *improves* provenance: it stops destroying the loser's text and keeps the relationship reversible (human-gated, explicit, reversible — exactly Principle V). All new reads/writes stay scoped by `(org_id, user_id)`; edges already carry tenancy.

Net: one pre-existing deviation (IV), documented and bounded. No new violations.

## Project Structure

### Documentation (this feature)

```text
specs/003-fact-rejection-lifecycle/
├── plan.md                         # This file
├── research.md                     # Phase 0 — reconciled decisions (post-refactor)
├── data-model.md                   # Phase 1 — facts/fact_edges lifecycle + edge kinds
├── quickstart.md                   # Phase 1 — build/run/verify (Postgres-only)
├── contracts/
│   └── candidate-lifecycle-api.md  # Phase 1 — changes to /candidates + /contradictions
└── checklists/
    └── requirements.md             # from /speckit-specify
```

### Source Code (repository root)

```text
knowledge/
├── knowledge_graph/
│   ├── knowledge_graph_def.py            # FactState: "decayed" → "rejected"; doc comment
│   ├── knowledge_graph_variants/
│   │   ├── postgres_vector_graph.py      # _overwrite → non-destructive; edge kind flip; set_state strings
│   │   └── vector_graph.py               # in-memory state string "decayed" → "rejected"
│   └── write_policy/
│       ├── write_policy_def.py           # retirement-state comment
│       └── write_step_variants/
│           └── conflict_overwriter.py    # action/supersede semantics → keep+reject+link (no text overwrite)
└── serve/
    ├── schema.sql                        # state column comment decayed→rejected
    ├── facts_candidates.py               # reject()/resolve()/resolve_custom()/delete(): rename + non-destructive + gate + hasOtherContradictions + re-approval
    ├── app.py                            # delete route 409 gate; resolve/approve response fields; /contradictions pending filter
    ├── contradiction_adapter.py          # serialize_pairs: surface status (pending|resolved)
    ├── pipeline_adapter.py               # fact_to_candidate: rival/state projection incl. resolved edges
    └── data/candidates.json              # fixture state strings decayed→rejected

migrations/
└── m2026_06_23_reject_rename.py          # one-shot idempotent UPDATE facts SET state='rejected' WHERE state='decayed' (+ cached_facts)

frontend-react/src/
├── types/candidate.ts                    # CandidateState: decayed→rejected (ApiConflictError already present)
├── api/candidateModel.ts                 # state→class/label maps
├── components/StateBadge.tsx             # badge label/class
├── components/layout/FilterBar.tsx       # state filter option/tabs
├── components/viz/legendConfig.ts        # legend label
├── components/ContradictionPanel.tsx     # per-fact: pending vs resolved + state-gated action
├── components/ContradictionsReview.tsx   # global pending view
└── api/{mockProvider,localLogsProvider}.ts + *.test.ts  # fixture/test state strings

knowledge/evals/cases/decayed_lesson_ignored*/   # → rejected_lesson_ignored* (ids + state strings)
```

**Structure Decision**: Existing web-app layout. No new top-level directories and **no new API routes** — the lifecycle is delivered by extending the existing facts-backed `/candidates` and `/contradictions` routes plus the `FactsCandidates` facade. Backend lands first; the frontend rename + contradiction UX follows.

## Key Design Decisions (see research.md for rationale)

1. **Extend existing routes, not a parallel API.** `/candidates?state=`, `/candidates/{id}` (+ `promote`/`reject`/`delete`), `/contradictions`, and `/contradictions/{pair}/resolve` are already facts-backed via `FactsCandidates`. The lifecycle is delivered by changing their behavior, not adding `/facts…` twins.
2. **Two edge kinds in `fact_edges`.** Keep `kind='contradiction'` for **pending** (no winner chosen) and add `kind='contradicted_by'` for **resolved** (winner active, loser rejected). Canonical-ordered single row (`sorted((a,b))`). Resolving **flips the kind in place** instead of `remove_edge` — this is the central fix for FR-004 (the link must survive resolution and stay reversible). The global pending view (FR-013a) is then `WHERE kind='contradiction'`.
3. **Non-destructive resolution in two places.** (a) `PostgresVectorGraph._overwrite` (the `/insights` path) stops rewriting the loser's `text`; the approved fact is a plain `add` at `state='active'`, each conflict is set `rejected` + linked. (b) `FactsCandidates.resolve` flips the edge kind rather than deleting it. The loser's `text`/`embedding` are never touched (SC-001).
4. **Deletion gating** is a precondition check in `FactsCandidates.delete` (and surfaced at the route): `state ∈ {proposed, rejected}` → delete (edges cascade via `ON DELETE CASCADE`); `active` → **HTTP 409** "reject the fact before deleting". The frontend already models `ApiConflictError { statusCode: 409 }`.
5. **`hasOtherContradictions`** (FR-008) is computed per newly-rejected fact at action time: an edge touching the fact exists *other than* the just-resolved A↔B row. Returned in the reject/approve/resolve responses; drives the review notice (SC-007).
6. **Re-approval (FR-010)** flips a `rejected` fact to `active` and demotes its currently-active contradictor to `rejected`, keeping the `contradicted_by` row (direction is implied by state). Delivered by extending `promote`/`resolve` to accept a rejected→active transition, not a new endpoint.
7. **Rename is data-only + migration-file.** `facts.state` is bare `text` (no enum/CHECK), so the rename is `UPDATE facts SET state='rejected' WHERE state='decayed'` (idempotent), placed in a new `migrations/m2026_06_23_reject_rename.py` following the established migration convention, and the same applied to `cached_facts`. No read-time shim (no data to preserve).

## Complexity Tracking

| Deviation | Why it exists | Why a simpler path was rejected |
|-----------|---------------|----------------------------------|
| **Principle IV (Offline-First) not satisfied** — server stays Postgres-only; no JSON fallback for the lifecycle. | Inherited from commit `dbf60d9`, which deleted the JSON candidate store and made `create_app()` require a DSN, *before* this feature. | Re-adding an offline facts store + edge store solely to satisfy IV for this feature is a large, out-of-scope rebuild of what the refactor intentionally removed. Tracked for resolution via a constitution-amendment follow-up: [docs/proposals/2026-06-23-principle-iv-offline-first-amendment.md](../../docs/proposals/2026-06-23-principle-iv-offline-first-amendment.md) (path (a)). |
