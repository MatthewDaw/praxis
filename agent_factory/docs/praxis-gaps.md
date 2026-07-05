# Knowledge-Graph Side: What Praxis Owns, and the Holes to Improve

> First-pass partition. This document covers **only the "knowing" system** — the
> capabilities from [`agent-coding-factory-reference.md`](./agent-coding-factory-reference.md)
> that should live in the knowledge graph because they are about durable, cross-session
> knowledge, retrieval, and truth-maintenance. For each, it marks whether Praxis already
> **covers** it, **partially** covers it, or has a **hole** we'd want to improve on the
> Praxis side. The companion doc [`factory-local-components.md`](./factory-local-components.md)
> covers everything we build locally instead.
>
> Status is graded against the current Praxis doc, not a fresh source audit — step 2's
> research team should confirm the HOLE/PARTIAL calls against `../praxis` before we commit.

---

## The boundary rule

A capability belongs on the **Praxis side** when it is about *knowledge that must outlive a
single session and be reasoned over* — stored facts, how they're retrieved, how they're
deduped/merged, how conflicts are resolved, how they age. It belongs on the **local side**
when it is about *running code, the agent loop, or ephemeral task state*. Code itself is
never KG data (it lives in git); only judgments, decisions, and learnings *about* the code do.

---

## What Praxis already covers well (lean on these, don't rebuild)

| Reference capability | Praxis mechanism | Status |
|---|---|---|
| Persistent cross-session knowledge store | Atomic facts under `(org_id, user_id)` tenancy | ✅ Covers |
| Hybrid retrieval (semantic + exact/keyword) | pgvector + BM25 fused via RRF in `/context` | ✅ Covers |
| Dedup + additive merge of facts | distillation → dedup → Mem0-style merge/augment | ✅ Covers |
| Contradiction detection + resolution | two-stage (structural slot + semantic) engine | ✅ Covers |
| **Gated writes** (don't let contradictions corrupt memory) | invalidate-and-keep + opt-in auto-resolution | ✅ Covers — this is a standout strength |
| Temporal validity / point-in-time recall | bitemporal `valid_at`/`invalid_at` + `as_of` | ✅ Covers |
| Read-time knowledge composition without contamination | mountable read-only snapshot overlays | ✅ Covers |
| Knowledge checkpoint / rollback | snapshots (`save` / `load`) | ✅ Covers |
| Coarse namespace scoping (global vs project) | `shared` flag + per-project principal + mounts | ✅ Covers |
| Per-fact provenance (source/score) | returned on every `/context` hit | ✅ Covers |

This is most of the *knowing* system. The reference model's hardest, least-solved subsystem
(gated memory writes with contradiction detection — the thing commercial products mostly
*don't* ship) is exactly where Praxis is strongest.

---

## The holes — knowledge-side capabilities the reference wants that Praxis lacks or only partially does

These are candidate improvements to **Praxis itself** (or, where noted, things we may have to
shim locally if Praxis can't take them on).

### H1. Outcome / trust feedback on facts — **✅ SHIPPED (merged PR #73)**
The reference wants verification outcomes to feed back into fact trust: down-weight or retire
a fact whose suggested action repeatedly *failed*, so retrieval sharpens on what demonstrably
worked (the actual compounding mechanism).
→ *Shipped:* `success_count`/`failure_count` on facts + a `Fact.utility` multiplier
(Laplace-smoothed success rate, neutral 1.0 until outcomes exist), folded into retrieval ranking;
`record_outcome(fact_id, success)` on the graph + `POST /facts/{id}/outcome`. Red-spec evals:
`praxis/.../matt/outcome_trust_*` (proven fact outranks a repeatedly-failed one).

### H2. Query-time scope/namespace filtering — **✅ SHIPPED (merged PR #80)**
Reference: scope retrieval so service A's auth never surfaces service B's payments. Praxis
hits *carry* `scope`/`category`, and mounts/tenancy give coarse partitioning, but it's unclear
`/context` can **constrain** a query to a scope/category server-side (the documented params are
`query`, `top_k`, `as_of`). If not, we over-retrieve and filter client-side.
→ *Shipped* (with H4, [`proposal`](../../praxis/docs/proposals/2026-06-25-episodic-memory-h4.md)):
an additive `exclude_categories` predicate on `search`/`_where` (`category <> ALL`, both cosine +
keyword branches, in-memory graph, and the live+mounted overlay union); `/context` default-excludes
`"episodic"` with an `include_episodic` override; write-time recall excludes episodic too. Eval:
`praxis/.../matt/context_excludes_episodic`. (Server-side exclusion now exists; the broader
cross-service scoping the reference described is the same knob, generalizable later.)

### H3. Automatic temporal decay / staleness expiry — **✅ SHIPPED (PR #83)**
Memory-safety best practice expires stale entries to shrink the poisoning/stale-recall surface.
→ *Shipped* ([`proposal`](../../praxis/docs/proposals/2026-06-25-temporal-decay-h3.md)):
retrieval ranking scales the cosine score by a recency factor `exp(-ln2*age/half_life)` on
`created_at` (half-life 90d) — multiplies with H1's utility, so `score = similarity * utility *
recency`. Neutral (~1.0) for fresh facts (no regression); **retrieval only** — suppressed for
`as_of` recall and *not* applied to write-time dedup/conflict recall (must still find old
near-dups). No schema change (reuses `facts.created_at`). Red-spec eval:
`praxis/.../matt/recency_decay_stale_loses`.
→ *Follow-up (deferred):* confirmation-refresh — a `last_confirmed_at` bumped by
`record_outcome(success)` so a re-validated old fact resets its recency ("confirmed in N runs").

### H4. Episodic vs. semantic vs. procedural memory types — **✅ SHIPPED (episodic; merged PR #80)**
The reference distinguishes *semantic* (durable facts), *episodic* (a timestamped decision +
its rationale + what happened), and *procedural* (reusable workflow templates). Praxis stores
flat atomic facts; the three types can be faked with `scope`/`category`, but there's no
first-class episodic record ("at plan-time we chose X because Y; it later failed"). `as_of`
helps reconstruct *what was believed*, not *why it was decided*.
→ *Shipped* (episodic only; procedural deferred — [`proposal`](../../praxis/docs/proposals/2026-06-25-episodic-memory-h4.md)):
convention, not a new type — an episode = a fact tagged `category="episodic"` + `meta.episode`
{decided_at, alternatives, outcome} + `derived_from` edges (H5), written via `record_episode`
(graph) / `POST /insights` with `category="episodic"`. Kept out of semantic recall by H2's exclude.
The load-bearing edge case is **built**: episodes run a **store-only lane** that bypasses the
semantic write pipeline — no atomization (stored whole), no dedup/merge, no
contradiction/supersession (append-only/immutable), and excluded from write-time recall of
semantic writes. A reserved-tag guard blocks a normal write from using `category="episodic"`.
Red-spec evals: `praxis/.../matt/episodic_store_only_immutable`, `episodic_reserved_tag_integrity`,
`episodic_stale_not_in_context`.

### H5. Richer typed edges / derivation links — **✅ SHIPPED (merged PR #77)**
The reference's compounding loop wants **derivation provenance**: "learning L was derived from
facts F1, F2," so when F1 flips you can find every downstream learning that's now suspect.
→ *Shipped:* `record_derivation` / `write(derived_from=[ids])` writes `derived_from` edges
(reusing `fact_edges`, no schema change); `dependents()` is a cycle-guarded, depth-bounded
recursive-CTE traversal; the reject chokepoint flags the transitive dependent closure with a
`derived_source_invalidated` review edge; `stale_derived()` surfaces the suspect learnings.
Red-spec eval: `praxis/.../matt/derivation_stale_source`.

### H6. Ingestion integrity on tabular/templated input — **✅ SHIPPED (merged PR #65)**
The merge path cut the old silent-near-duplicate drop, but tabular input leaked at **two**
independent points (now both fixed). (Full design: [`../../praxis/docs/proposals/2026-06-24-tabular-ingestion-integrity.md`](../../praxis/docs/proposals/2026-06-24-tabular-ingestion-integrity.md).)
- **A — distillation under-emits:** the splitter collapses rows sharing a sentence shape; the
  offline path can't parse tables at all.
- **B — the deduper over-merges siblings:** the `MergeJudge` folds distinct-but-similar rows into
  one fact. Note `/insights` skips A but still hits B.
→ *Shipped:* (1) deterministic table-linearizer; (2) a **dedup slot-guard** keyed on
the full functional `(subject, attribute)` slot from the `claims` table — **not** subject alone
(subject-only fails the same-subject/different-attribute shape, e.g. a role×permission table,
which our PRD has). The guard is a three-way decision: distinct slot → block merge; same slot +
different value → route to contradiction engine; same slot + same value → merge (idempotency).
Missing/empty claim ⇒ demote to `proposed` (fail toward distinct). #1 and #2 must ship together —
#1 alone only makes tables *look* fixed.
→ *Meanwhile* shim locally (table-linearization + rejected-pile audit — see local doc); the
slot-guard (B) **cannot** be shimmed and must land in Praxis.
→ *Eval:* `praxis/.../matt/augment_no_merge_distinct_rules` — the **prose analog** of loss-point B,
found live (the admission-rule vs. done-gate-rule planning facts were silently over-merged on seed).
Distinct rules that merely share vocabulary must not be collapsed.

### H7. Retrieval budget / tier controls — **✅ IMPLEMENTED (branch `feat/retrieval-tuning-h7`)**
The reference wants per-tier token budgets and the ability to bias semantic-vs-keyword weight
per query type (concept vs symbol). Praxis token-bounds results (~8KB) and fuses via RRF, but
exposed no knobs to tune the fusion or budget per call.
→ *Shipped (optional, additive, all defaulting to the calibrated behavior — no behavior change
when unset):*
- **Budget knob** — `char_budget` on `graph.read` (`PostgresVectorGraph` + `OverlayGraph`),
  overriding the module-global `_READ_CHAR_BUDGET` (~8KB) per call. A smaller budget keeps the
  reader prompt tight.
- **Fusion-weight knob** — `keyword_weight` threaded through `search` → `_rrf_fuse` (semantic
  branch fixed at 1.0, keyword branch scaled by this value). Raise it for a symbol/exact-match
  query, lower it to lean semantic. Only meaningful with the existing `hybrid=True`.
- **HTTP surface** — `/context` now accepts `hybrid`, `keyword_weight`, and `char_budget` query
  params (fusion knobs apply to the live graph; the mounted-snapshot union stays cosine-only).
- *Contract:* `keyword_weight`/`hybrid` added to the `SearchableGraph` abstract signature;
  in-memory `VectorGraph` accepts them as no-ops (no keyword branch to fuse).
→ *Test:* `praxis/knowledge/tests/test_rrf_fusion_weight.py` — pure-function unit tests proving a
raised `keyword_weight` promotes the keyword-branch winner, weight 0 == pure semantic, and the
default reproduces the historical ranking. (Not a `case.yaml` — this is a ranking-mechanism knob,
not a graph-state assertion.)

### H8. Bulk write throughput / synchronous read-your-writes — **PARTIAL**
`/ingest` is slow/async (minutes); a just-written learning isn't immediately retrievable.
`/insights` is synchronous and lower-loss, which mitigates this for shaped facts. Not a
correctness hole, but a latency constraint the local loop must design around.
→ *Praxis improvement (nice-to-have):* faster/confirmable writes; *meanwhile* local staging.

### H9. Detect-without-auto-resolve write mode — **✅ SHIPPED (merged PR #75; verified 2026-06-25)**
The plan-hardening loop needs contradictions **surfaced for a human**, not silently settled.
Originally `add_insight` auto-resolved every conflict (newest wins, loser → `rejected`, nothing in
`get_contradictions`). **Fixed in Praxis:** `add_insight`/`ingest` now take
`on_conflict="surface" | "auto_resolve"` (default `auto_resolve`). With `surface`, a conflict keeps
both facts (incumbent `active`, newcomer `proposed`, neither rejected) and raises a **pending pair
in `get_contradictions`** settled by `resolve_contradiction`.
- *Verified live:* `retry count is 3` then `...7` with `on_conflict="surface"` → both kept (3 active,
  7 proposed), one pending pair, neither rejected; `resolve_contradiction(keep_id=7)` superseded 3.
- *Consequence:* the earlier rejected-pile workaround is **retired**; `af-intake` and the
  knowledge-port policy (`docs/af-memory-policy.md`) now use `on_conflict="surface"` +
  `get_contradictions` as the surface.

### H10. Semantic-contradiction precision — **✅ SHIPPED (merged PR #74; verified 2026-06-25)**
The semantic detector over-flagged compatible facts (e.g. "knowledge is stored in the KG" vs "code
is never in the KG"). Tightened in Praxis; the pair now coexists with no contradiction. Eval cases
`praxis/.../matt/semantic_no_conflict_storage_target` **and**
`praxis/.../matt/semantic_no_conflict_distinct_actors` (different-actors variant, found live in
the roles cluster — captain-approval vs. coach-immediate) pin it. *Note:* the conflict-checked
write runs an inline semantic-judge LLM call and can **time out client-side after the write
succeeds** — consumers must read back rather than blind-retry (handled in the knowledge-port
policy, `docs/af-memory-policy.md`); the
latency/timeout itself is tracked in **H13**.

### H11. No "dismiss / keep-both" contradiction resolution — **✅ SHIPPED (merged PR #79)**
When a surfaced contradiction is a **false positive** (the engine flagged two facts that both
actually hold — e.g. the captain-approval vs. coach-immediate pair, different actors), there is no
non-lossy way to clear it. `resolve_contradiction` offers only `keepId` (supersedes one — loses a
true fact) or `customText` (replaces both with one — forces a lossy merge of two distinct facts).
Neither preserves two distinct, compatible facts.
**Implemented:** `POST /contradictions/{id}/resolve {"dismiss": true}` flips each pending
`contradiction` edge among the members to a new `dismissed` kind (preserved + reversible, not
deleted — the human decision stays discoverable) and forces **both** facts `active`. A `dismissed`
edge is neither `contradiction` (pending) nor `contradicted_by` (resolved/superseded), so the pair
drops out of `get_contradictions` while both stay in recall. The one intentional override of
FR-005's ≤1-active-contradictor rule, on explicit human judgement. Wired end to end:
- **Backend** — `resolve` route dismiss branch + `FactsCandidates.resolve_dismiss` (returns
  `{"dismissed": true, "facts": [...]}`).
- **MCP** — `praxis_resolve_contradiction(..., dismiss=True)`.
- **UI/services** — `contract_v1.build_resolve_body` (new `RESOLUTION_DISMISS`), `api_client`
  (handles the `{"dismissed", "facts"}` shape), `data_provider` protocol, and `mock_provider`
  (`_dismiss_contradiction` keeps both active, clears cross-flags).
→ *Test:* `test_resolve_dismiss_keeps_both_active_and_clears_pending` in
`knowledge/serve/tests/test_server.py` — **green** (full serve suite 24/24, MCP 47/47, frontend mock
17/17). The precision fix for the false-positive *class* is H10; H11 is the escape hatch for the
residue precision can't eliminate.
→ *Local workaround now retired:* no longer need the lossy `customText`/`keepId`-and-re-add dance.

### H12. Write-time metadata not persisted/honored — **✅ SHIPPED (merged PR #81)**
`add_insight` previously accepted `source`/`scope`/`category` but didn't honor them and had no
`meta` arg, which blocked H2/H4. **Shipped:** the write paths now persist writer-supplied
`source`/`scope`/`category` + a `meta` jsonb arg and round-trip them back on `/context`/`candidates`
(writer value wins; ingestion-derived fills only unset fields). Red-spec:
`test_insight_persists_writer_metadata`. (Original analysis kept below for context.)
This had blocked three things at once:
- **Provenance citation** (the factory must cite which fact grounded a decision — needs `source`).
- **H2** (exclude by `category`) and **H4** (tag episodes `category="episodic"` + `meta.episode`)
  both *assume* the writer can set `category`/`meta` — they are **blocked on H12**.
**To build (no schema change — `facts` already has `source`/`scope`/`category`/`meta` columns):**
1. **Persist writer-supplied `source`/`scope`/`category`** on the write paths (`POST /insights`,
   `POST /ingest`, MCP `praxis_add_insight`/`praxis_ingest`) into the existing columns, and
   **return them** on `/context` hits and `/candidates` (today they come back null).
2. **Accept a `meta` (jsonb) arg** on the same paths → persist to the `meta` column → return it
   (on `/candidates` at minimum; see open question on `/context`).
3. **Precedence:** writer-set value **wins**; ingestion-derived `scope`/`category` fill in **only
   when the writer left the field unset** (never clobber an explicit tag — H4's `"episodic"` tag
   depends on this).
4. **Round-trip contract (the whole point):** a value written is the value read back, unchanged.

**Blocks:** H2 (filter by `category`) and H4 (episode tag `category="episodic"` + `meta.episode`)
both assume settable `category`/`meta`; both are **blocked until H12 lands**. Do this first.
→ *Eval:* pytest red-spec `test_insight_persists_writer_metadata` in
`knowledge/serve/tests/test_server.py` (`case.yaml`'s `direct_to_graph` is plain strings — can't
set per-fact metadata, so this must be a write-path round-trip test):
```python
def test_insight_persists_writer_metadata(client):
    r = client.post("/insights", json={
        "insight": "The team day resets at 03:00 local time.",
        "source": "prd-team-app", "scope": "prd-team-app",
        "category": "requirement", "meta": {"requirement_id": "R4"}})
    assert r.status_code == 200, r.text
    hit = next(h for h in client.get("/context", params={"query": "team day reset"}).json()["hits"]
               if "03:00" in h["text"])
    assert hit["source"] == "prd-team-app"          # RED today: null
    assert hit["scope"] == "prd-team-app"
    assert hit["category"] == "requirement"
    cand = next(c for c in client.get("/candidates").json() if "03:00" in c["content"])
    assert cand.get("meta", {}).get("requirement_id") == "R4"
```
→ *Open questions:* (a) return `meta` on every `/context` hit or only `/candidates`? (lean:
`source`/`scope`/`category` on hits as today's keys, `meta` on `/candidates` to keep `/context`
lean). (b) writer-vs-derived `category` precedence (lean: writer always wins, derived fills unset).

### H13. Write-path reliability under load — **PARTIAL (H13.1 timeout shipped in PR #77; concurrency + membership remain)**
Three operational failures hit live during the dry-run, all on the conflict-checked write path:
1. **Client-side timeouts:** a write that triggers the inline semantic-judge LLM call routinely
   times out at the MCP client *after the write has already committed server-side* — a false
   negative that invites duplicate retries.
2. **Write-burst fragility:** ~3–8 concurrent `add_insight` calls drove the backend to 500 on all
   writes (then reads), needing a restart.
3. **Org-membership not durable across restart:** after a backend restart / token refresh,
   membership in a created org vanished (`whoami` showed none), forcing org re-creation.
**To build (each independent):**
1. **Timeout — ✅ SHIPPED (PR #77):** per-call MCP HTTP timeouts (long for writes/ingest, short
   for reads) cover the inline semantic-judge round-trip. *Stretch (still open):* make
   conflict-checking async so writes return fast and the pending contradiction surfaces shortly
   after — deferred unless the bump proves insufficient.
2. **Concurrency** — the conflict-checked write path must tolerate concurrent writers without
   cascading 500s; investigate connection-pool exhaustion / a transaction left open under load.
   Minimum bar: a failing write fails *cleanly*, not poisoning the backend for unrelated requests.
3. **Membership durability** — org membership must survive a backend restart / token refresh
   (persisted, not in-memory). Re-creating the org on every restart is untenable for a factory
   that depends on durable snapshots (`planning-knowledge`, `prd-*`) living in that org.
→ *Not eval-able as `case.yaml`* (infra/latency/concurrency/persistence, not a graph-state
   assertion) — these want a concurrency stress test (1,2) and a restart-survival integration test (3).
→ *Priority:* **H13.1 (timeout)** next after H12 — most disruptive day-to-day. **H13.3
   (membership)** before we rely on durable snapshots (M2). **H13.2 (concurrency)** lowest — the
   local interim below mitigates it.
→ *Meanwhile (local, in control):* the knowledge-port policy (`docs/af-memory-policy.md`) mandates **serial** conflict-checked writes
   (never parallel bursts) + **read-back-and-re-add on timeout** (a timeout ≠ failure; the write
   usually committed — read back and only re-add if absent, never blind-retry).

---

## Summary (status as of 2026-06-25)

**✅ Shipped & merged (10 of 13):**
- **Compounding loop — complete:** **H1** (outcome→trust, #73), **H4** (episodic memory, #80),
  **H5** (derivation edges, #77). These turn "a store of facts" into "memory that gets *more
  accurate*, not just bigger."
- **H6** (tabular ingestion integrity, #65) — the immediate blocker, fixed.
- **H2** (query-time category exclusion, #80) — server-side, completes H4's filter.
- **H12** (writer metadata round-trip, #81) — the keystone H2/H4 depended on.
- **Write-path cluster from the 2026-06-25 smoke test:** **H9** (surface-mode conflicts, #75),
  **H10** (semantic-contradiction precision, #74), **H11** (dismiss/keep-both resolution, #79).
- **H13.1** (per-call MCP write timeout, #77).

**Recently shipped:** **H3** (temporal decay, PR #83) and **H7** (retrieval-tuning knobs,
`feat/retrieval-tuning-h7`).

**Remaining (1 partial + infra), all "sharpening/ergonomics" — workable around short-term:**
- **H8** (bulk-write throughput / read-your-writes latency) — latency, not correctness; shimmed.
- **H13.2** (write-burst concurrency) and **H13.3** (org-membership durability across restart) —
  infra reliability; not eval-able as `case.yaml` (want a stress test + a restart-survival test).

Praxis now covers the whole compounding loop, recency/decay, retrieval tuning, and the write-path
reliability basics. What's left is H8 (write latency, shimmed) and the two H13 infra items
(concurrency, membership durability).
