# Quickstart: REJECTED state + retained-contradiction lifecycle

How to build, run, and verify this feature locally. Backend first; dashboard second.
(Post-refactor baseline: the server is **Postgres-only** — there is no JSON/offline path.)

## Prerequisites

- `uv` (Python ≥3.12). Backend tests run offline against fakes (`FakeEmbedder`, no-LLM policy),
  but the server process itself needs a Postgres DSN.
- Node + the `frontend-react` toolchain (Vite + Vitest) for the dashboard.
- A Postgres 16 + pgvector instance. Set `PRAXIS_DB_URL=postgresql://…` (local dev commonly on
  `:5433`).

## Phase 1 — backend

### 1. Apply schema + the rename migration

```bash
# Idempotent schema bootstrap (extension + tables)
uv run python -m knowledge.serve.db
# One-shot, idempotent data rename: decayed -> rejected (facts + cached_facts)
uv run python -m migrations.m2026_06_23_reject_rename
```

### 2. Run the backend test suite

```bash
uv run pytest knowledge -q
# Targeted:
uv run pytest knowledge/knowledge_graph/write_policy/tests/test_conflict_overwriter.py -q
uv run pytest knowledge/serve/tests/test_facts_candidates.py -q
uv run pytest knowledge/serve/tests/test_server.py -q
```

### 3. Verify the core behavior (TDD order — write these FAILING first)

Per Constitution Principle II (test-first, non-negotiable):

1. **Retention regression** — an approved fact that contradicts an `active` fact leaves the loser's
   `text` unchanged, sets it `rejected`, and creates a `contradicted_by` edge. (Replaces the current
   `_overwrite`/`test_conflict_overwriter` expectation that text is rewritten.)
2. **Resolve keeps the link** — `POST /contradictions/{pair}/resolve` flips the edge
   `contradiction`→`contradicted_by` instead of deleting it; the resolved pair is still discoverable
   from either fact.
3. **Delete gating** — `DELETE` an `active` fact → 409; `proposed`/`rejected` → 200 and edges gone.
4. **State swap** — re-approving a `rejected` loser flips both states and keeps the link.
5. **Ripple flag** — a rejected fact with another contradiction reports `hasOtherContradictions: true`;
   the just-resolved pair alone reports `false`.
6. **Migration** — a seeded `decayed` row reads back as `rejected`.

### 4. Smoke the (extended) endpoints

```bash
uv run python -m knowledge.serve     # serves http://localhost:8000
# With a valid JWT + X-Praxis-Org header:
#   GET    /candidates?state=rejected
#   GET    /candidates/{id}                 # contradictions incl. status: pending|resolved
#   POST   /candidates/{id}/reject          # -> hasOtherContradictions
#   POST   /candidates/{id}/promote         # proposed->active, and rejected->active (re-approval)
#   DELETE /candidates/{id}                 # 409 if active
#   GET    /contradictions                  # pending pairs (kind='contradiction')
#   POST   /contradictions/{pair}/resolve   # flips edge, keeps link
```

## Phase 2 — dashboard

### 1. Rename `decayed` → `rejected`

Touch-points in `frontend-react/src`: `types/candidate.ts`, `api/candidateModel.ts`,
`components/StateBadge.tsx`, `components/layout/FilterBar.tsx`, `components/viz/legendConfig.ts`, plus
`mockProvider`/`localLogsProvider` and their `*.test.ts`. `ApiConflictError { statusCode: 409 }` is
already defined for delete-on-active.

### 2. Contradiction review UX

State-filter tabs (Proposed · Active · Rejected · All); a per-fact contradiction panel
(`ContradictionPanel.tsx`) that shows pending vs resolved with a state-gated Reject/Approve action; and
the global pending view (`ContradictionsReview.tsx`, FR-013a). Reflect state changes without a manual
refresh (FR-017) by refreshing affected facts after each action.

### 3. Run frontend gates

```bash
cd frontend-react && npm test && npm run lint && npm run build
```

## Done / acceptance

- `uv run pytest knowledge -q` green, including the new retention/resolve-link/gating/swap/ripple/
  migration tests; renamed `rejected_lesson_ignored*` eval cases pass.
- The string `decayed` no longer appears in user-facing labels/filters/state values (SC-006).
- Frontend gates green; manual walk-through of User Story 1–3 acceptance scenarios against Postgres.
