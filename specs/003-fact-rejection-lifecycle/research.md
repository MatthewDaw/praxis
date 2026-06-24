# Phase 0 Research: REJECTED state + retained-contradiction lifecycle

This feature is an *integration* with existing code, so "research" here is grounding decisions
in the current implementation. This file was **reconciled after commit `dbf60d9`** ("collapse
knowledge graph onto a single facts spine"), which invalidated the original plan's premises.
No `NEEDS CLARIFICATION` markers remain after `/speckit-clarify`.

## Decision 0 — Reconcile with the single-facts-spine refactor (supersedes the original plan)

**What changed under us** (commit `dbf60d9`, verified in code):

- The separate `candidates` table and its stores (`CandidateStore` / `PostgresCandidateStore`,
  the old `store.py` / `postgres_store.py`) were **deleted**. The dashboard candidate surface is
  now `FactsCandidates` ([facts_candidates.py:70](../../knowledge/serve/facts_candidates.py)),
  a read-model projected directly over `facts` (candidate id == fact id).
- The server is **Postgres-only**: `create_app()` opens one shared autocommit connection and
  requires a resolvable DSN ([app.py:76-82](../../knowledge/serve/app.py)). The JSON/offline
  path and its 503 behavior are gone.
- `fact_edges` is **actively used**: edges are persisted on write (`_persist_contradictions`),
  read via `all_edges`/`active_edges`, and removed via `remove_edge`. It is no longer
  "schema-only".
- `/candidates` (with `?state=`), `/candidates/{id}` (+ `promote`/`reject`/`delete`),
  `/contradictions`, and `/contradictions/{pair}/resolve` are **already facts-backed**.
- One-off data migrations now live in a `migrations/` package
  (`m2026_06_23_unify_facts.py`), alongside the idempotent `schema.sql` bootstrap.

**Consequences for this feature** (each reverses an original-plan assumption):

| Original plan said | Reconciled decision |
|--------------------|---------------------|
| Add a new parallel `/facts…` API | **Extend the existing `/candidates` + `/contradictions` routes** — they already read facts. |
| `fact_edges` is unused, schema-only | It is the live contradiction store; build on it. |
| New routes return 503 on the offline JSON path; mirror `_strip_link` | No offline path exists; `ON DELETE CASCADE` handles edge cleanup. `_strip_link` is gone. |
| Dashboard Phase 2 "repoints" from candidates → facts | Already done by the refactor; remaining FE work is the rename + pending/resolved UX. |
| Rename via `UPDATE` folded into `schema.sql` | Use a `migrations/` file (the new convention) for the data update. |

## Decision 1 — Where the lifecycle lives: `facts` + `fact_edges` via `FactsCandidates`

**Decision**: Keep the lifecycle on the `facts` table and contradiction links in `fact_edges`,
surfaced through `FactsCandidates` and the existing routes.

**Rationale**: This is already the source of truth post-refactor; no new persistence is needed.
`facts.state` (`schema.sql`) is a bare `text` column (no enum/CHECK), so the state rename is pure
data. `fact_edges` PK is `(org_id, user_id, src_id, dst_id, kind)` with FKs `ON DELETE CASCADE`.

**Alternatives considered**: A new `/facts…` API over the same table (rejected — duplicates the
already-facts-backed candidate routes; violates Simplicity/YAGNI and Principle III's "one contract").

## Decision 2 — Edge representation: add `contradicted_by`, flip instead of delete

**Decision**: Two `kind` values on the canonical-ordered row. `contradiction` = pending
(no winner); `contradicted_by` = resolved (winner active, loser rejected). Resolving **flips the
kind in place**; it does not delete the row.

**Rationale**: The spec requires the resolved relationship to remain discoverable from either fact
and be reversible (FR-004, FR-007, SC-003). Today `FactsCandidates.resolve`
([facts_candidates.py:237](../../knowledge/serve/facts_candidates.py)) calls `remove_edge` —
**destroying the link** the spec wants kept. Flipping the kind preserves it and makes the global
pending view a trivial `WHERE kind='contradiction'` query (FR-013a). `_rival_map`
([facts_candidates.py:94](../../knowledge/serve/facts_candidates.py)) currently reads only
`kind='contradiction'`; it must read both kinds so resolved contradictions still show in the
per-fact view (FR-012).

**Alternatives considered**: Keep deleting the edge and infer resolution from state (rejected —
loses the link, breaks reversibility and the audit trail). Two directed rows per pair (rejected —
more writes, no query benefit; breaks existing canonicalization).

## Decision 3 — Non-destructive resolution replaces in-place overwrite (two call sites)

**Decision**: Remove text destruction from both resolution paths.

- **`/insights` path**: `ConflictOverwriter.apply`
  ([conflict_overwriter.py](../../knowledge/knowledge_graph/write_policy/write_step_variants/conflict_overwriter.py))
  sets `action='overwrite'` + `update_target_id` + `supersede_ids`; `PostgresVectorGraph._overwrite`
  ([postgres_vector_graph.py:729-755](../../knowledge/knowledge_graph/knowledge_graph_variants/postgres_vector_graph.py))
  runs `UPDATE facts SET text=…` on the nearest conflict and `state='decayed'` on the rest. Change
  this so the approved fact is a plain `add` at `state='active'`; **every** conflict (including the
  former `update_target_id`) is treated uniformly as a loser: `state='rejected'`, text untouched,
  `contradicted_by` edge upserted.
- **Reviewer path**: `FactsCandidates.resolve` stops calling `remove_edge`; it sets the loser
  `rejected`, the winner `active`, and flips the edge kind to `contradicted_by`.

**Rationale**: FR-003 / SC-001 forbid destroying the loser's content. The loser's `text`/`embedding`
are never modified.

**Note on `/insights` read-back**: `app.py` infers the reported action by comparing the top fact
before/after. Because the approved fact is now always a fresh `add`, that read-back must still report
a sensible action (likely "added" with rejected-loser info) and should surface
`hasOtherContradictions`.

## Decision 4 — `decayed` → `rejected` rename + migration

**Decision**: `FactState = Literal["proposed", "active", "rejected"]`
([knowledge_graph_def.py:13](../../knowledge/knowledge_graph/knowledge_graph_def.py)); update every
`set_state(..., "decayed")` call, the in-memory `vector_graph.py`, the `_overwrite` SQL, the
`schema.sql` column comment, the `write_policy_def.py` comment, the JSON fixture, and the frontend
labels/filters/types. Apply a one-shot idempotent data update via a new
`migrations/m2026_06_23_reject_rename.py`: `UPDATE facts SET state='rejected' WHERE state='decayed'`
and the same for `cached_facts`. No read-time shim.

**Rationale**: Bare `text` columns make this pure data. The `migrations/` package is the
established convention post-refactor (mirrors `m2026_06_23_unify_facts.py`). No production rows to
preserve (clarification 2026-06-23), so a maintained compatibility layer is unjustified.

**Eval cases**: `decayed_lesson_ignored` and `decayed_lesson_ignored_reader`
([knowledge/evals/cases/](../../knowledge/evals/cases/)) must be renamed to `rejected_*` (ids +
embedded state strings) and continue to assert real shipped behavior (Principle I), not a tuned
constant.

## Decision 5 — Deletion gating & cascade

**Decision**: `FactsCandidates.delete` checks the fact's state: `active` → raise a precondition
error surfaced as **HTTP 409** ("reject the fact before deleting"); `proposed`/`rejected` → delete
the row. Edge cleanup is automatic (`fact_edges … ON DELETE CASCADE`). Today `delete`
([facts_candidates.py:219](../../knowledge/serve/facts_candidates.py)) does **no** state check — this
adds it.

**Rationale**: FR-014/FR-015/FR-016. The frontend already defines `ApiConflictError { statusCode: 409 }`
([types/candidate.ts](../../frontend-react/src/types/candidate.ts)), so the contract is anticipated
client-side.

## Decision 6 — `flags` vs `fact_edges` duplication

**Decision**: `fact_edges` is the single source of truth (already the case post-refactor); the
transient `Fact.flags = ["contradiction:<id>"]` is derived/legacy and not relied upon by the
lifecycle reads. The passive flagger path is unchanged (out of scope).

**Rationale**: FR-018. Avoids two divergent records; the refactor already moved reads onto edges.

## Decision 7 — Concurrency

**Decision**: Apply each resolution atomically and re-check the invariant ("never both active") at
write time; last action wins; no user-facing conflict UX (clarification 2026-06-23). psycopg
connections are autocommit; multi-row resolves run in a single statement/transaction so the invariant
is never observably broken.

**Rationale**: Per-`(org,user)` graphs see little concurrency; DB-level serialization is sufficient
and simplest.

## Decision 8 — Offline-first deviation (Constitution Principle IV)

**Decision**: Stay Postgres-only for this feature; do **not** re-add a JSON/offline lifecycle path.

**Rationale**: Commit `dbf60d9` deleted the offline candidate store and made the server require a DSN
*before* this feature. Re-adding an offline facts + edge store is a large rebuild outside this spec's
scope. Documented as a bounded deviation in [plan.md](plan.md) Complexity Tracking and flagged for a
separate follow-up rather than silently accepted.

## Open items deferred to `/speckit-tasks` / implementation

- Exact transaction boundary for multi-row resolves under the autocommit connection (single
  `UPDATE … WHERE id = ANY(...)` vs explicit `BEGIN`).
- Whether `/insights` reuses a shared non-destructive resolve helper or keeps its own write path.
- Per-case decision on renaming vs re-authoring the `decayed_lesson_ignored*` eval cases.
