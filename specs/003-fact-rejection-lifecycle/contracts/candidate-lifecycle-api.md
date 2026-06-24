# Contract: Candidate/Contradiction lifecycle API (changes to existing routes)

Post-refactor (`dbf60d9`), the dashboard API in [app.py](../../../knowledge/serve/app.py) is
**already facts-backed** via `FactsCandidates`. This feature **extends the existing routes** — it
adds no `/facts…` API. All routes carry the existing auth (Cognito JWT `current_user` + org
membership via `X-Praxis-Org`); tenancy is `(org, principal.sub)`. The server is Postgres-only, so
there is no offline/503 variant.

Conventions: `409` = state-precondition violation; `404` = unknown id; `400` = bad request.

State value rename across **every** route: `decayed` → `rejected` (FR-001, SC-006).

Contradiction `status` ⇔ edge `kind` (single mapping, used everywhere below): `status="pending"` ⇔
`kind='contradiction'` (no winner approved — at least one side not yet rejected); `status="resolved"`
⇔ `kind='contradicted_by'` (a winner is `active`, the loser `rejected`).

---

## `GET /candidates?state=<state>` — *unchanged shape, renamed value*

List the tenant's facts, optionally filtered by lifecycle state. Backs the "review by state" tabs
(FR-011).

- Query: `state` ∈ `proposed | active | rejected` (omit for all). **`rejected` replaces `decayed`.**
- 200 → `[{ id, title, content, state, confidence, provenance, contradictions: [...] , ... }]`
- **Change**: the `state` field returns `rejected`, never `decayed`.

## `GET /candidates/{cid}` — *per-fact contradictions gain status*

Fetch one fact, including the facts it contradicts (FR-012).

- 200 → the candidate object; its contradiction list MUST include **both pending and resolved**
  relationships, each annotated with the contradictor's `state` and a `status` ∈ `pending | resolved`
  (derived from edge `kind`).
- 404 if `{cid}` unknown.
- **Change**: `_rival_map` must read both `contradiction` and `contradicted_by` edges (today it reads
  only `contradiction`), and the projection must carry `status` per rival.

## `POST /candidates/{cid}/reject` — *adds `hasOtherContradictions`*

Manually move `{cid}` to `rejected` (reversible; distinct from delete — FR-016).

- Body: `{ reason?: string }`
- 200 → `{ ...candidate, state: "rejected", hasOtherContradictions: bool }`
- 404 if unknown.
- **Change**: state string `rejected` (was `decayed`); response adds `hasOtherContradictions` (FR-008).

## `POST /candidates/{cid}/promote` — *allows re-approval of a rejected fact (FR-010)*

Approve a fact to `active`.

- Body: `{ targetState?: "active" }`
- `proposed → active` (unchanged today).
- **New**: `rejected → active` re-approval — flips `{cid}` to `active`, demotes its currently-active
  contradictor to `rejected`, keeps the `contradicted_by` edge, and returns the demoted fact's
  `hasOtherContradictions`.
- 200 → `{ ...candidate, state: "active", rejected?: [{ id, hasOtherContradictions: bool }] }`
- 404 if unknown; 400 if the transition is illegal.

  > **Decision (final)**: FR-010 re-approval is carried by `promote` (the per-fact "Approve" action
  > acts on a fact id, which maps cleanly to this route). The `/contradictions/{pair}/resolve`
  > alternative is not used for re-approval.

## `DELETE /candidates/{cid}` — *adds state gating (FR-014, FR-015)*

Hard-delete a `proposed` or `rejected` fact; refuse on `active`.

- 200 → `{ deleted: cid }`
- **409** if the fact is `active` → `{ detail: "reject the fact before deleting" }`.
- 404 if unknown.
- On success all `fact_edges` touching `{cid}` are removed (cascade); the fact no longer appears in
  any other fact's contradiction list (SC-005).
- **Change**: today `delete` does no state check — add the precondition.

## `GET /contradictions` — *pending only / link survives resolution*

Global list of contradictions for the tenant. Backs the global pending-contradictions view (FR-013a).

- 200 → `[{ pair_id, a: {...}, b: {...}, status }]`
- **Change**: the global pending view lists pairs where `kind='contradiction'` (one side still not
  rejected). Resolved pairs (`contradicted_by`) are discoverable per-fact (via `GET /candidates/{cid}`)
  but drop out of the pending list once resolved.

## `POST /contradictions/{pair_id}/resolve` — *flip edge, don't delete it*

Resolve a contradiction by keeping one side.

- Body: `{ keepId?: string, customText?: string }` (`pair_id` is `"<a>__<b>"`).
- 200 → `{ ...keptCandidate, hasOtherContradictions: bool }` for the rejected loser(s).
- **Change (central)**: today `resolve` calls `remove_edge`, **destroying the link**. It must instead
  set the loser `rejected`, the winner `active`, and **flip the edge `contradiction` → `contradicted_by`**
  so the relationship survives and is reversible (FR-004, SC-003). The loser's `text` is never modified
  (SC-001). `resolve_custom` (decay-both + fresh fact) similarly must keep an auditable link rather than
  dropping edges silently.
- 404 if `pair_id` unknown.

---

## Behavioral contract tests (map to spec acceptance scenarios)

1. `/insights` approve of B contradicting active A → A `rejected` with **text intact**, B `active`,
   `contradicted_by` edge present (US1 #1, SC-001). *Replaces the current overwrite assertion.*
2. Approve over several conflicts → all `rejected` + linked, none overwritten (US1 #2).
3. Approve over a `proposed` (never-live) conflict → it becomes `rejected` + linked (US1 #3, FR-006).
4. `GET /candidates/{cid}` returns both pending and resolved contradictions with correct
   `state`/`status` (FR-012).
5. `GET /contradictions` lists every pending pair; resolving one flips its edge and drops it from the
   pending list while it stays visible per-fact (US2 #6, FR-013a, FR-004).
6. Re-approve a rejected loser → states swap, pair still linked via `contradicted_by` (US2 #3, FR-010,
   SC-003).
7. Reject/approve where the affected fact has another contradiction → `hasOtherContradictions: true`;
   the just-resolved A↔B link alone → `false` (US2 #4/#5, SC-007).
8. `DELETE` an `active` fact → 409; `DELETE` a `proposed`/`rejected` fact → 200, edges gone, absent
   from contradictors' lists (US3 #1–#3, SC-005).
9. Seeded legacy `decayed` row reads back as `rejected` after the migration (FR-002, SC-006).
