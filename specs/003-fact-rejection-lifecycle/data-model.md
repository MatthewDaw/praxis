# Phase 1 Data Model: REJECTED state + retained-contradiction lifecycle

No new tables. The lifecycle is expressed on the existing `facts` table and the already-active
`fact_edges` pivot. (Post-refactor baseline — commit `dbf60d9` — there is no `candidates` table.)

## Entity: Fact (`facts` table — existing)

| Field | Type | Notes / change |
|-------|------|----------------|
| `id` | text | composite PK with `(org_id, user_id)` |
| `org_id`, `user_id`, `shared` | text / bool | tenancy; unchanged |
| `text` | text | the fact content — **must never be overwritten on a loss** (FR-003) |
| `source`, `confidence`, `scope`, `category`, `observation_count` | various | unchanged |
| `state` | text | **`proposed` \| `active` \| `rejected`** — value `decayed` renamed to `rejected`. Bare `text`, no CHECK/enum. |
| `embedding` | vector(1536) | unchanged; not touched on rejection |
| `meta`, `created_at` | jsonb / timestamptz | `meta` carries dashboard fields (`title`, `auditTrail`, `supersedes`); unchanged shape |

**Type change** ([knowledge_graph_def.py:13](../../knowledge/knowledge_graph/knowledge_graph_def.py)):
`FactState = Literal["proposed", "active", "rejected"]` (was `"decayed"`). Update the doc comment
block above it and the `state` column comment in `schema.sql`.

### State transitions

```
                approve (direct / /insights ingest at active)
   proposed ───────────────────────────▶ active
      │                                     │
      │ (loses a contradiction)             │ (loses a contradiction / manual reject)
      ▼                                     ▼
   rejected ◀───────────────────────────  rejected
      │   ▲                                 ▲
      │   └── re-approve (flip) ────────────┘   (former winner → rejected; edge stays)
      │
      └── delete (row removed; edges cascade)

proposed ── delete ──▶ (removed)        active ── delete ──▶ 409 REFUSED (reject first)
```

- **proposed → active**: direct approval (`promote`) / `/insights` ingest at `state="active"`.
- **active|proposed → rejected**: loses an approved contradiction (FR-003, FR-006), or manual reject
  (`POST /candidates/{id}/reject`).
- **rejected → active**: re-approval (FR-010); the currently-active contradictor is demoted to
  `rejected`; the `contradicted_by` edge persists; pair stays linked.
- **delete**: allowed only from `proposed` or `rejected` (FR-014); removes the row and cascades all
  `fact_edges`.

**Invariant (FR-005, SC-002)**: no two facts joined by a contradiction relationship are both
`active`. Enforced atomically at write time (last writer wins).

## Entity: Contradiction relationship (`fact_edges` table — existing, actively used)

| Field | Type | Notes |
|-------|------|-------|
| `org_id`, `user_id` | text | tenancy |
| `src_id`, `dst_id` | text | the two facts; stored **canonically ordered** `sorted((a, b))`; FK → `facts` `ON DELETE CASCADE` |
| `kind` | text | **`contradiction`** (pending/unresolved) or **`contradicted_by`** (resolved: winner active, loser rejected) |

PK = `(org_id, user_id, src_id, dst_id, kind)`.

**Status ⇔ kind mapping** (canonical, used by the contract and the projection): the API `status`
field is `"pending"` ⇔ `kind='contradiction'`, and `"resolved"` ⇔ `kind='contradicted_by'`.

### Relationship rules

- **Pending** (`kind='contradiction'`): two facts conflict, no winner approved. Written on every
  conflicting fact write (`_persist_contradictions`). Backs the global pending view (FR-013a):
  `WHERE kind='contradiction'`.
- **Resolved** (`kind='contradicted_by'`): created by **flipping** the pending row's kind when an
  approval resolves the pair; the loser is `rejected`, the winner `active`.
- **Resolving** a pending contradiction = set loser `rejected` + flip edge `contradiction`→
  `contradicted_by` on the canonical row. **Change from today**: `FactsCandidates.resolve` currently
  `remove_edge`s the link ([facts_candidates.py:237](../../knowledge/serve/facts_candidates.py)) —
  that deletion is replaced by the flip so the link survives (FR-004).
- **Re-approval** of a rejected fact = swap which endpoint is `active`/`rejected`; the
  `contradicted_by` row persists (direction is implied by state — no row rewrite needed).
- **Deletion** of either endpoint removes all its edges (cascade).

### Read-path change: `_rival_map` must include resolved edges

`_rival_map` ([facts_candidates.py:94](../../knowledge/serve/facts_candidates.py)) currently iterates
only `all_edges("contradiction")`. For the per-fact view to show **both** pending and resolved
contradictions with correct status (FR-012), it must read both kinds and tag each rival with its
status (`pending` | `resolved`, derived from `kind`).

### Derived signal: `hasOtherContradictions` (FR-008)

Not stored. Computed per newly-rejected fact at action time:
`EXISTS (edge touching this fact) AND (some edge other than the just-resolved A↔B row exists)` →
boolean returned in the reject/approve/resolve response. Drives the review notice (FR-008, SC-007).

## Legacy / derived

- `Fact.flags = ["contradiction:<id>"]` — transient marker from the passive `ConflictFlagger`.
  Superseded by `fact_edges` as source of truth (FR-018); not consumed by the lifecycle reads.
- `cached_facts` / `cached_fact_edges` — snapshot/eval caches keyed by `cache_key`. The
  `decayed`→`rejected` data update applies to `cached_facts.state` too, for snapshot/eval continuity.
