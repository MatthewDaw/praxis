# Proposal: REJECTED state + retained-contradiction lifecycle

**Owner:** Dominic Antonelli — knowledge graph
**Status:** Proposed
**Date:** 2026-06-23
**Scope:** the fact lifecycle (`FactState`), the contradiction write path, and fact deletion. Backend = Phase 1; dashboard = Phase 2.
**Relates to:** the `facts`/`fact_edges` schema (`knowledge/serve/schema.sql`), `ConflictFlagger`/`ConflictOverwriter`, the candidate store (`knowledge/serve/store.py`), and the dashboard's Contradictions tab.

> Today a fact that loses a contradiction is **decayed and its text is destroyed** (`ConflictOverwriter` overwrites the loser in place). This proposal renames `decayed` → `rejected`, and changes the loser's fate: **keep both facts**, mark the loser `rejected`, and link the pair with a bidirectional `CONTRADICTED_BY` pointer so the contradiction stays auditable and reversible. `rejected` facts can later be hard-deleted; deletion cleans up the pointers. Facts can also be hard-deleted directly, skipping `rejected`.

---

## Phase 1 — Backend (data model + lifecycle)

### 0. Two stores: `facts` (source of truth) vs `candidates` (projection)

A **Candidate is not a separate record from a Fact — it is a dashboard-shaped projection of one.** `fact_to_candidate` renders a `Fact` into the UI model (id `pipe_<factid>`, plus title / confidence breakdown / audit trail) and copies the fact's `state` verbatim. The data flow:

```
distilled insights ──write policy──▶ FACTS (proposed)  ──project──▶ CANDIDATES (dashboard review)
 chat approval (/insights) ─────────▶ FACTS (active)
```

The catch: that projection is an **offline batch export** (`regenerate.py` → `export_pipeline_candidates`), not a live view. `CandidateStore` seeds from a JSON/`candidates`-table **snapshot** and then mutates independently (the Contradictions tab resolves *candidates*), while `/insights` mutates *facts* independently. The two stores share a state vocabulary but **drift**.

**Decision (settles the earlier open question):** this lifecycle — rename, contradiction pointers, deletion, state review — is defined on **`facts`** as the single source of truth. New endpoints (§5) read/write `facts`. The candidate layer is a read-projection; the legacy candidate-based Contradictions tab is **repointed at facts**, not extended in place. (Whether to also make the candidate snapshot a live projection of `facts` is a related cleanup, tracked but out of scope here.)

### 1. Rename `decayed` → `rejected`

`decayed` is the retirement/superseded state today; the request renames it to `rejected`. It is a pure rename of one enum value — no new state. Touch points (from a repo sweep for `decayed`/`DECAYED`):

| Layer | File(s) | Change |
|-------|---------|--------|
| Fact type | `knowledge_graph/knowledge_graph_def.py` | `FactState = Literal["proposed", "active", "rejected"]` + the doc comment |
| Write-policy type | `write_policy/write_policy_def.py` | comment referencing `decayed` as the retirement state |
| Postgres store | `knowledge_graph_variants/postgres_vector_graph.py` | `_overwrite` sets `state = 'rejected'` (and see §2) |
| Schema | `serve/schema.sql` | column comment; **no enum constraint exists** so no DDL value change is forced (`state` is bare `text`) |
| Candidate store | `serve/store.py` | `reject()` and `resolve()` set the loser `state = "rejected"` |
| Postgres candidate store | `serve/postgres_store.py`, `pipeline_adapter.py` | state strings |
| Front-end types | `frontend-react/src/types/candidate.ts`, `FilterBar.tsx`, legend/graph config | `CandidateState` union, the filter `<option>`, legend label (Phase 2) |
| Evals | `evals/cases/decayed_lesson_ignored*`, tests | case ids/text reference "decayed"; rename for consistency or leave the eval semantics and only swap the state string — decide per-case |

**Migration:** `facts.state` and `candidates.state` are free `text` columns with no `CHECK`/enum, so a rename is a data update, not a schema migration: `UPDATE facts SET state = 'rejected' WHERE state = 'decayed'` (same for `candidates`). Idempotent; safe to fold into `db.py :: bootstrap()`. **Decision:** keep the rename backward-compatible by having the read path treat a legacy `'decayed'` as `'rejected'` for one release, then drop the shim — or just run the one-shot `UPDATE` if there is no production data to preserve (confirm).

> **Recommendation:** do the `UPDATE` + comment/type edits in one commit and keep no compatibility shim if the deployed `facts` table has no rows worth preserving. Confirm before assuming.

### 2. Contradiction-on-accept: retain, don't destroy

This is the substantive change, not the rename.

**Today:** when an *approved* insight contradicts an existing fact, `ConflictOverwriter` turns the write into an `overwrite` — the new text **replaces the conflicting fact in place** and any further conflicts are set to `decayed`. The loser's original text is gone; only the survivor remains. (The passive path, `ConflictFlagger`, instead records a transient `contradiction:<id>` flag and keeps both, but never resolves.)

**Proposed:** when a fact that contradicts another is **ACCEPTED**, the contradicted fact is **kept** and moved to `rejected` (not overwritten, not deleted). Both facts get a **`CONTRADICTED_BY`** pointer to the other. The accepted fact stays `active`; the loser becomes `rejected`; the relationship is recorded so it can be reviewed and reversed.

> **"ACCEPTED" — settled.** The lifecycle is `proposed → active → rejected`. **ACCEPTED = the user-approval action that lands a fact in `active`** (a direct approval, the same trigger that today drives `ConflictOverwriter` / the `/insights` ingest at `state="active"`). It is *not* a new fourth state and is *not* a rename of `active`. "Accepted" and "active" are the same thing said two ways.

**Many-to-many — the pivot table already exists.** The schema already defines `fact_edges`:

```sql
CREATE TABLE fact_edges (
    org_id, user_id, src_id, dst_id,
    kind text NOT NULL DEFAULT 'contradiction',
    PRIMARY KEY (org_id, user_id, src_id, dst_id, kind),
    FOREIGN KEY (...src_id) REFERENCES facts (...) ON DELETE CASCADE,
    FOREIGN KEY (...dst_id) REFERENCES facts (...) ON DELETE CASCADE
);
```

It is **currently unused by application code** (only the schema, infra, and a README mention it — no INSERT/SELECT anywhere). It is exactly the many-to-many pivot the request anticipates, and its `ON DELETE CASCADE` gives us the §3 pointer-cleanup *for free*. So we don't add a table — we **wire the existing one**.

**Edge representation — decision:**
- `CONTRADICTED_BY` is symmetric ("these two contradict"); the **state** of each endpoint (`active` vs `rejected`) tells you which won. Store it as **one edge with `kind = 'contradicted_by'`** plus the existing `(src,dst)` ordering, and read it from both directions — or write two directed rows. A single canonical-ordered row (sorted `src < dst`) is simplest and matches how `serialize_pairs`/`graph_from_candidates` already canonicalize pairs with `sorted((a, b))`.
- Keep `kind = 'contradiction'` for the **pending/unresolved** flag (the `ConflictFlagger` signal, two facts that conflict but nobody has accepted a winner) and `kind = 'contradicted_by'` for the **resolved** relationship (a winner was accepted, loser rejected). Resolving a contradiction flips the edge kind and sets the loser `rejected`. *(Alternative: one `kind` and infer pending-vs-resolved from endpoint states. Slightly less explicit; either is fine — I recommend the two-kind split for queryability.)*

**Write-path change.** Replace the destructive `overwrite` in the approved-contradiction path with a non-destructive resolve:
- `ConflictOverwriter` (or a new `ConflictResolver` step) no longer emits `action = "overwrite"`. Instead it keeps the new fact as a normal `add` at `state = "active"`, sets each conflicting fact to `rejected`, and inserts a `contradicted_by` edge per pair.
- `decision.supersede_ids` semantics shift from "decay these" to "reject these + edge these." The in-place text replacement (`_overwrite`) is removed; `_add` runs as usual and the losers are updated separately.

This also **retires the lossy behavior** where an approved correction silently erased the prior wording — provenance is now preserved.

### 2b. Rejection ripples — flag a rejected fact that has *other* contradictions

A fact never lives in isolation: when an accept pushes fact **B** to `rejected`, B may already participate in **other** contradiction relationships (beyond the A↔B link that just caused the rejection). Rather than reason about each one, keep it simple: **if the newly-rejected fact has one or more *other* contradiction edges, tell the user they may want to review that fact's details.**

- It's a single boolean per rejected fact — "has other contradictions: yes/no" — not a per-edge analysis. We don't classify or chase the chain, and we **never auto-cascade** (no auto-resurrect, no auto-resolve); the user is simply pointed at the fact and decides.
- Mechanically: the `accept`/`reject` response includes the rejected fact id(s) with a `hasOtherContradictions` flag (true when an edge other than the just-created A↔B one exists). The Phase 2 UI turns a `true` into a notice that links to the rejected fact's detail. No flag / no notice when the rejected fact had no other contradictions.

### 3. Deleting a `rejected` fact cleans up pointers

`rejected` facts can be hard-deleted. Because `fact_edges` FKs are `ON DELETE CASCADE`, a `DELETE FROM facts WHERE id = ?` **automatically removes every `contradicted_by`/`contradiction` edge touching it** — no manual pointer cleanup needed in the Postgres path. The candidate (JSON) store has no cascade, so its delete must mirror the existing `_strip_link` logic (already present in `store.py`) to drop the reverse `contradiction_ids` entry from the other side.

### 4. Facts can be hard-deleted without being rejected first

Deletion is allowed from **any** state (`proposed`, `active`, `rejected`). It is a separate operation from rejection:
- **Reject** = state transition, reversible, keeps the row + pointers.
- **Delete** = row removal, irreversible, cascades pointers (§3).

A `DELETE /facts/{id}` (and the candidate-store equivalent — `DELETE /candidates/{cid}` already exists) covers this. No state precondition.

### 5. API surface (Phase 1)

| Endpoint | Purpose | Notes |
|----------|---------|-------|
| `GET /facts?state=…` | list facts filtered by state | backs Phase 2 "review by state"; mirror the existing `GET /candidates?state=` |
| `GET /facts/{id}/contradictions` | the facts this fact contradicts (+ their state) | reads `fact_edges`; backs the Phase 2 contradiction panel |
| `POST /facts/{id}/accept` | accept → `active`, reject contradictors, write edges | the §2 resolve action; response flags any rejected fact with §2b `hasOtherContradictions` |
| `POST /facts/{id}/reject` | move a fact to `rejected` | manual reject without an opposing accept |
| `DELETE /facts/{id}` | hard-delete from any state | cascades edges (§3) |

All new endpoints act on **`facts`** (the §0 decision). The candidate store reuses the same response shapes as a projection.

### 6. Invariants

- **Two contradicting facts can never both be `active`** — that is the invariant the whole flow protects. Therefore **accepting** a `rejected` fact (Phase 2 "Approve" button) **automatically rejects its contradictor** (the symmetric flip) — *settled: the flip is automatic*, and the newly-rejected fact gets the §2b `hasOtherContradictions` check.
- **Re-accepting a loser:** approving a `rejected` fact flips it `active` and pushes the former winner to `rejected`, swapping the edge direction. The pair stays linked; only states/edge-direction change. The demoted winner gets the §2b check (it may have other contradictions worth a look).
- **Multi-way contradictions:** one accepted fact may reject several facts (`supersede_ids` is already a list). The `contradicted_by` pivot handles the resulting many-to-many cleanly; each rejected fact gets its own §2b flag.
- **`proposed` losers:** a contradicted fact that was only `proposed` (staged, never live) still becomes `rejected` — *settled* — for a uniform audit trail, rather than being silently dropped. The same `contradicted_by` edge and §2b check apply.
- **`flags` vs `fact_edges` duplication:** the transient `flags: ["contradiction:<id>"]` on the `Fact` model overlaps with the `contradiction` edge kind. **Recommendation:** `fact_edges` becomes the single source of truth and `flags` is derived, to avoid two divergent contradiction records.

### Phase 1 verification

1. Accepting fact B that contradicts active fact A → A is `rejected`, B is `active`, a `contradicted_by` edge links them; **A's text is intact** (regression test against the old overwrite behavior).
2. `DELETE` A → edge is gone (`fact_edges` empty for A), B unaffected, B no longer reports A under `GET /facts/{id}/contradictions`.
3. `DELETE` an `active` fact directly → succeeds (no rejection precondition).
4. Re-accept a `rejected` loser → states swap, pair still linked.
5. Ripple flag: accept C over B where B already had a contradiction with some D → response marks B `hasOtherContradictions: true` (and the A↔B-style C↔B link alone does not trip it); a rejected fact with no other edges reports `false`.
6. Migration: a seeded `'decayed'` row reads back as `'rejected'`.

---

## Phase 2 — Front-end

Two new capabilities are requested: **(A) review facts by state**, and **(B) from a fact, see the facts it contradicts, each with an Approve/Reject action chosen by their state.** Both read **`facts`** (the §0 decision), so the legacy candidate-backed Contradictions tab and state filter are repointed at the new `/facts…` endpoints. The dashboard already has a state filter (`All/proposed/active/decayed`) in `FilterBar.tsx` and a `Contradictions` tab that resolves pairs by "keep one, decay the loser" — Phase 2 evolves both. First, rename the `decayed` option/label to `rejected` to match Phase 1.

### B — action button by state

The contradicted-fact list shows one action per row, gated on the row's current state and the viewing fact's state:

| Contradicted fact's state | Action shown | Effect |
|---------------------------|--------------|--------|
| `active` or `proposed` | **Reject** | move it to `rejected`; keep this fact accepted (writes/keeps the `contradicted_by` edge) |
| `rejected` | **Approve** | re-accept it → `active`; flips this fact to `rejected` (the swap from §6) |

Each row links to the contradicted fact and shows its state badge so the asymmetry is legible.

**Ripple notice (§2b).** When an Approve/Reject flips a fact to `rejected` and that fact has *other* contradictions, the response sets `hasOtherContradictions`. The UI raises a simple notice — e.g. a toast "*X* was rejected and has other contradictions — review it?" linking to the rejected fact's detail. No flag ⇒ silent success; nothing auto-cascades.

### Layout options for A + B

Three reasonable layouts; they differ in *where* the contradiction review lives, not in the underlying endpoints.

**Option 1 — State tabs + per-fact contradiction drawer** *(recommended)*
Promote the existing state `<select>` into top-level tabs (`Proposed · Active · Rejected · All`) over the fact table. Clicking a fact opens a side drawer with its detail; if it has contradictions, the drawer shows a **"Contradicted facts"** section listing each with its state badge and an Approve/Reject button.
- *Pros:* reuses the table users already know; contradictions are reviewed in the context of a specific fact (matches the request's framing exactly: "the list of Facts contradicted by **this** Fact"). State review (A) and contradiction review (B) share one surface.
- *Cons:* contradictions are only discoverable by opening the owning fact — no global "all unresolved contradictions" roll-up unless we keep the existing Contradictions tab too.

```
┌ Proposed · [Active] · Rejected · All ─────────────┐   ┌ Fact detail ───────────┐
│ ▸ Use uv, not pip           active                │   │ "Use uv, not pip"       │
│ ▸ Deploy via script.sh      active   ⚠ 1          │──▶│ state: active           │
│ ▸ Tabs over spaces          rejected              │   │                         │
│ ▸ …                                                │   │ Contradicted facts:     │
└────────────────────────────────────────────────────┘   │ • "Use pip"  rejected   │
                                                          │            [ Approve ]  │
                                                          └─────────────────────────┘
```

**Option 2 — Dedicated "Contradictions" review tab, grouped by winner**
Keep contradiction review as its own tab (evolve the current one). List each accepted fact that has contradictions, with its rejected/contradicted facts nested beneath, each row carrying an Approve/Reject action. State review (A) stays as the table's existing filter.
- *Pros:* one place to triage every contradiction; closest to the current Contradictions tab, lowest disruption; good when contradictions are the primary review task.
- *Cons:* two separate surfaces for "by state" (table filter) and "contradictions" (tab); the "contradicted by this fact" view is a grouping, not anchored to browsing a fact.

```
Contradictions
  ▼ "Use uv, not pip"  (active)
       contradicts  "Use pip"            rejected   [ Approve ]
  ▼ "Tabs over spaces" (active)
       contradicts  "Spaces over tabs"   rejected   [ Approve ]
       contradicts  "2-space indent"     proposed   [ Reject  ]
```

**Option 3 — Single table, inline state column + expandable contradiction rows**
One fact table with a `state` column and a state filter; rows that have contradictions get an expand chevron that reveals the contradicted facts inline with per-row Approve/Reject buttons. No drawer, no separate tab.
- *Pros:* everything on one screen, fewest navigations; density suits power users scanning many facts.
- *Cons:* inline expansion gets cramped with multi-way contradictions; mixes "browse" and "act" in the same row, which can be error-prone for a destructive-ish action.

```
State ▾ [All]      Search […]
┌─────────────────────────────────────────────┐
│ ▾ Use uv, not pip                 active     │
│     ↳ contradicts Use pip   rejected [Approve]│
│   Deploy via script.sh            active     │
│ ▾ Tabs over spaces                active     │
│     ↳ contradicts 2-space   proposed [Reject] │
└─────────────────────────────────────────────┘
```

> **Recommendation:** Option 1. It maps 1:1 onto the request ("review by state" = tabs; "facts contradicted by *this* fact" = the per-fact drawer section), reuses the existing table, and keeps the destructive Approve/Reject action behind a deliberate click into a specific fact. If a global triage roll-up is also wanted, keep the Option 2 Contradictions tab alongside it — they share the same endpoints.

### Phase 2 verification

- State tabs/filter list the right facts per state (incl. `rejected`); legend/labels say "rejected", not "decayed".
- A fact with contradictions shows them with correct state badges; the action button is `Reject` for `active`/`proposed` rows and `Approve` for `rejected` rows.
- Approve on a rejected contradictor flips both states and the UI reflects the swap without a manual refresh.
- An accept that rejects a fact with *other* contradictions raises the §2b notice linking to that rejected fact; an accept where the rejected fact has no other contradictions shows no notice.
- Deleting a fact removes it from its contradictors' lists.

---

## Out of scope

- The contradiction **detection** quality (the LLM `ConflictJudge`) — unchanged.
- `active`/`proposed` semantics and the passive `ConflictFlagger` default — only the *resolution* path changes.
- Any rename of `active` → `accepted` (pending the §2 confirmation).
