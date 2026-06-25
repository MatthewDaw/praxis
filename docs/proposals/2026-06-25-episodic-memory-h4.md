# Proposal: Episodic / Decision Memory (H4) + Query-time Exclusion (H2)

**Status**: Draft · **Raised**: 2026-06-25 · **Owner**: TBD
**Source**: agent-factory gaps **H4** and **H2** (`agent_factory/docs/praxis-gaps.md`). H4 is
the last item in the compounding-loop cluster (after **H1** and **H5**, both shipped); **H2**
(query-time scope/category exclusion) is folded in here because it is the natural completion of
H4's filter — H4 tags episodes, H2 makes the store exclude them server-side. Part 1 below is
H4; **Part 2** is H2.

---

## Problem (hit live, not hypothetical)

The factory's plan-building dry-run writes **decision notes** — "PRD was silent on the daily
reset, so default to reset-to-0"; "the §1/§6 tension resolves toward X because Y" — as plain
facts. Two things go wrong:

1. They get **atomized** by distillation like any other fact, fragmenting one decision into
   several rows.
2. They **pollute semantic retrieval**: a query like "how does the daily reset work" now
   surfaces the *meta*-note "PRD was silent, so we defaulted…" alongside (or above) the actual
   answer. The "why we decided" record competes with the "what is true" record.

Praxis stores flat atomic *semantic* facts. It has no notion of an **episodic** record — a
timestamped decision + its rationale + what later happened — that can be kept *out of the way*
of semantic recall while remaining queryable on its own. `as_of` reconstructs *what was
believed* at a time, not *why it was decided*. That missing record is H4, and the pollution
above is the concrete cost of not having it.

---

## What already exists (so H4 is mostly convention, not code)

- **`write()` already carries the tagging + metadata an episode needs**
  ([postgres_vector_graph.py](../../knowledge/knowledge_graph/knowledge_graph_variants/postgres_vector_graph.py)):
  `write(text, *, scope=…, category=…, meta=…)` — `meta` persists to the `meta` jsonb column;
  `category`/`scope` persist as columns.
- **H5 gives derivation edges** — `write(..., derived_from=[ids])` / `record_derivation` links a
  record to the facts it was based on (`derived_from`), and an invalidated basis already
  surfaces dependents via `stale_derived()`.
- **Retrieval filtering is equality/include-only.** `search(query, *, filters, scope, …)` →
  `_where` builds `scope = %s` / `category = %s` predicates (≈ the `_where` method). There is
  **no exclusion** predicate (`category != …`). So "keep episodes out of semantic recall"
  cannot be a server param today — that exclusion is gap **H2**, deliberately deferred.

Net: an episode is *storable today* as `write(decision_text, category="episodic",
meta={…}, derived_from=[basis_ids])`. What's missing is the **convention** (so episodes are
tagged consistently and distinctly) and the **filter** (so the tag actually keeps them out of
semantic use).

---

## Decisions (locked)

1. **Convention, not a new type — but episodes BYPASS the semantic write pipeline.** An episode
   is a normal fact's *storage shape* (a row + `derived_from` edges + `meta`), but it must **not**
   go through the transforms a semantic fact does. An episodic log is **append-only and
   immutable**; the semantic pipeline would destroy it (see "Episodes bypass the write pipeline"
   below). Zero new tables; revisit a dedicated `episodes` type only if querying gets awkward.
2. **Tag distinctly at write time; then exclude server-side (H2, now in scope).** The durable
   decision is the **reserved tag** on every episode from creation. Originally we deferred the
   filtering to a client-side drop; this plan now **also completes H2**, so exclusion lands
   server-side in `/context` retrieval. The client-side `drop_episodic` (§Part 1.3) ships as the
   interim/fallback and as the H4 red-spec, but H2 (Part 2) is the real fix and supersedes it.
3. **Episodic only.** No procedural (workflow-template) memory — different shape, not
   load-bearing for the compounding loop.
4. **Harness-emitted.** The factory loop writes episodes (it knows the decision + rationale +
   basis it retrieved); Praxis just stores/serves them. Same thin-Praxis split as H1/H5.
5. **Storage + red-spec first; wire H1/H5 later.** Ship the convention + the failing eval that
   proves the pollution, then connect outcomes (H1) and basis edges (H5) in a follow-up.

---

## What I'm building

### 1. The episode convention (the spec)

An **episode** is a fact written with:

- **`category = "episodic"`** — the reserved tag. (`category`, not `scope`: `scope` is for
  service/dir namespacing; `category` is already a kind label — `error_fix | constraint | …` —
  so `"episodic"` slots in naturally and is distinct from every semantic category.)
- **text** = the decision + its rationale, as one self-contained statement
  ("Chose reset-to-0 for the daily habit counter because the PRD was silent and 0 is the safe
  default").
- **`meta`** = structured extras:
  ```json
  {
    "episode": {
      "decided_at": "2026-06-25T...Z",
      "alternatives": ["carry-over streak", "no reset"],
      "outcome": "pending"            // later: "succeeded" | "failed" (H1 tie-in)
    }
  }
  ```
- **`derived_from` edges** (H5) → the facts/requirements the decision was based on, so an
  invalidated basis surfaces the decision via `stale_derived()` for free.

This is a documented convention (in this doc + the eval), not a schema.

### 1b. Episodes bypass the semantic write pipeline (REQUIRED — the load-bearing edge case)

An episode is stored *like* a fact but must skip the four transforms a semantic write runs,
each of which is fatal to an append-only decision log:

- **No distillation/atomization.** Store the decision+rationale **whole**, as one row. (The
  fragmentation called out in the Problem is *not* fixed by the tag alone — `record_episode`
  must write verbatim, never through the splitter.) `meta.episode` attaches to the one row.
- **No dedup/merge.** Two decision notes on the same topic ("chose reset-to-0…", later "chose
  carry-over…") must both survive — they are a timeline, not duplicates to collapse.
- **No contradiction/supersession.** A later decision must **never** flag or supersede an earlier
  one. "We decided X at T" stays true forever, even after reversal — that is what
  `meta.episode.outcome="failed"` records. Episodes are never `invalid_at`/`rejected` by the
  engine.
- **Excluded from write-time recall of *semantic* writes.** When a normal fact is written, the
  dedup/conflict detector recalls candidates; episodes must be excluded from that candidate set
  so a new semantic fact is never merged-with or contradiction-flagged-against an episode. (This
  is the write-side of H2's exclusion — see Part 2.4.)

Implementation: route `category == "episodic"` writes down a **store-only path** (persist row +
`meta` + `derived_from` edges; skip distiller, deduper, augmenter, conflict detector,
supersession). The reserved tag is what selects the lane. This is the one place the
"episode = normal fact" framing must not be taken literally.

### 1c. Reserved-tag integrity

Because `category="episodic"` now routes writes to the store-only lane and out of semantic
recall, it is load-bearing. Make `"episodic"` a named constant, reserve it, confirm no existing
category emits it, and **reject (or namespace) a non-episode write that tries to use it** — else
a stray semantic fact tagged `"episodic"` silently vanishes from recall.

### 2. A thin producer helper (ergonomics only)

`record_episode(text, *, alternatives=None, outcome="pending", derived_from=None, decided_at=None)`
on the graph — a one-liner over the store-only episode write (§1b) with `meta={"episode": {…}}`
and `derived_from=…`. Optional sugar so callers don't hand-assemble the `meta` shape.

**The producer must be reachable from MCP, or H4 has no live writer.** Decision #4 makes the
*factory harness* the producer, and it writes through MCP — but today `praxis_add_insight`'s
`category` is not honored and there is no `meta` arg, so the harness **cannot write a proper
episode**. A graph-only `record_episode` leaves H4 as storage nothing can populate (only the eval
injects episodes). So this change must **also** expose the producer over MCP: either a
`praxis_record_episode` tool, or — at minimum — honor `category` and accept `meta` on
`praxis_add_insight` (routing `category="episodic"` to the store-only lane). Without this, H4 is
inert for its stated producer.

### 3. The client-side filter — build ONLY if H4 ships before H2

A post-retrieval drop (`drop_episodic(hits)`) that excludes `category == "episodic"` hits when
assembling semantic context. **Minimize-features note:** because H2 (Part 2) is now in the *same*
proposal and is the real fix, this client drop is **vestigial** — building both the interim shim
and the server-side exclude in one release is wasted scaffolding. **Default: skip `drop_episodic`
and rely on H2's server-side exclusion.** Only build it if H4 lands in a release *before* H2; in
that case it's a tiny shared util (so the eval and harness share one definition) and H2 supersedes
it. Either way, the episode log stays readable via the `category == "episodic"` equality query.

### 4. (Deferred to follow-ups, not this change)

- **H1 tie-in:** `record_outcome` on an episode flips `meta.episode.outcome` and feeds trust.
- **H5 is already wired** via `derived_from`; nothing extra needed now.
- **Procedural memory** (workflow templates) stays out of scope.
- *(H2 server-side exclusion is now IN scope — see Part 2.)*

---

---

# Part 2 — H2: query-time scope/category exclusion (the real filter)

## Problem

`search()` and `_where` are **include-only**: `scope = %s`, `category = %s`, and each
`filters` entry becomes `AND <key> = %s`. There is no way to say "retrieve everything *except*
this category." So `/context` (the MCP/agent retrieval entrypoint) cannot omit episodes
server-side — which is exactly why H4 needed a client-side drop. H2 adds the missing exclusion
so the store itself keeps episodic (and, generally, any out-of-scope) rows out of a query.

## What I'm building

### 1. An exclusion predicate on the retrieval path

- **`search(query, *, exclude_categories: list[str] | None = None, …)`** — threaded into
  `_where` as `AND (category IS NULL OR category <> ALL(%s))` (NULL category = never excluded).
  Applied to **both** the cosine and keyword branches, since they share `_where`. Equality
  `filters`/`scope` (include) stay as-is; this is the additive *exclude* counterpart.
- Generalizes beyond episodes: the same param later supports "service A's query never surfaces
  service B's facts" (the original H2 motivation), so we build the general knob, not a
  one-off `hide_episodic` flag.

### 2. `/context` excludes episodes by default

The agent/MCP retrieval entrypoint (`/context` → `praxis_get_context`) defaults to
`exclude_categories=["episodic"]`, so the "why we decided" records never pollute semantic
recall — server-side, for every consumer, with no client cooperation. An explicit opt-in
(`include_episodic=true` on the route / tool arg) clears the exclusion when a caller genuinely
wants decisions in the mix. The **episode-log surface** is the include counterpart: query
`filters={"category": "episodic"}` to read decisions on their own.

### 3. Supersedes H4's client-side drop

Once `/context` excludes server-side, the harness no longer needs `drop_episodic` — Part 1's
client drop becomes a redundant fallback. The H4 *convention* (the tag) is what made this
migration-free: H2 is purely additive on top of the tag that already exists.

### 4. Wiring

`search` → reader (`RetrievingReader` already forwards `filters`/`scope`; add `exclude_categories`
to the `ReadRequest`) → HTTP `/context` (default-exclude + `include_episodic` override) → MCP
`praxis_get_context`. Optional index on `facts (category)` to keep the predicate cheap.

**Apply the exclude across mounted overlays.** Episodes are facts, so a mounted snapshot's
episodes ride into `/context` via the `UNION ALL`. The `exclude_categories` predicate must apply
to the **unioned** (live + mounted) result, not just the live branch — otherwise a mounted
snapshot's decision notes pollute recall.

**Write-time recall must also exclude episodic (the write-side of this filter).** This is the
other half of §1b's "excluded from write-time recall." When a *semantic* fact is written, the
dedup/conflict detector's candidate recall must pass `exclude_categories=["episodic"]` so a new
fact is never merged-with or contradiction-flagged-against an episode. So the default-exclude is
**not** only at `/context` — it is: (a) defaulted at the agent-facing `/context` route, AND (b)
applied to the internal recall of *semantic* writes. Episode writes themselves run no recall at
all (store-only lane, §1b). The only place that sees episodic rows is the explicit episode-log
query (`filters={"category":"episodic"}`) and `include_episodic=true`.

## Storage impact

| Piece | Touches | New table? |
|---|---|---|
| episode = fact + `meta.episode` + `category="episodic"` | existing `facts` (`meta`, `category`) | **No** |
| basis links | existing `fact_edges` `derived_from` (H5) | No |
| keep-out-of-semantic filter (interim) | post-retrieval drop in the consumer | No |
| episode-log read | existing equality `filters` on `category` | No |
| **H2** exclude predicate | `exclude_categories` param → `_where` (`category <> ALL`) | No |
| **H2** optional speedup | index on `facts (category)` | index, not a table |

No new database, table, column, or migration. Both H4 (convention) and H2 (one additive
predicate + param) are minimal-feature: they reuse `facts.category`/`meta` and the existing
`search`/`_where`/reader plumbing.

---

## The red-spec evals (prove the live pollution)

**H4 — store-only lane + immutability** (`matt/episodic_excluded_from_semantic/`):
1. Seed a real **semantic** fact and a multi-sentence **decision-note episode** on the same
   topic, both `active`; the episode is tagged `category="episodic"`.
2. Assert the episode is stored **whole** (one row, not atomized) with `meta.episode` intact, and
   is retrievable via the episode-log query (`category="episodic"`).
3. Write a **second** decision-note episode on the same topic; assert **both** episodes persist
   (no dedup/merge) and neither is `rejected`/superseded (no contradiction) — the log is append-only.
4. Assert a semantic write on the same topic is **not** flagged as contradicting an episode.
5. *(Only if `drop_episodic` is built — see §3)* applying it removes the episode from a semantic
   hit list. Otherwise this exclusion is covered entirely by the H2 red-spec below.

**H2 — server-side exclusion** (`matt/context_excludes_episodic/`):
1. Same seed.
2. Call retrieval **with `exclude_categories=["episodic"]`** (no client drop at all).
3. Assert the semantic fact is returned and the episode is absent from the result — proving the
   store itself excluded it.

**RED today:** episodes written as plain facts ride in semantic results, and `search` has no
exclude param. **GREEN** once (H4) episodes are tagged + dropped and (H2) `_where` honors
`exclude_categories` and `/context` defaults to excluding `"episodic"`. Both directly encode the
pollution you are hitting live.

---

## Sequencing

1. **H4: store-only episode lane (§1b) + reserved tag (§1c) + `record_episode` (graph + MCP, §2)
   + H4 red-spec** — episodes are written whole, immutable, MCP-reachable, and tagged. (Skip
   `drop_episodic` unless this ships before step 2.)
2. **H2: `exclude_categories` on `search`/`_where`/reader (live + mounted) + `/context`
   default-exclude + `include_episodic` override + write-time-recall exclusion for semantic
   writes (Part 2.4) + H2 red-spec** — the real server-side filter; the pollution goes
   RED → GREEN here.
3. **Factory-harness wiring** (agent_factory repo) — write decisions as episodes; rely on
   `/context` server-side exclusion (drop the client `drop_episodic` once H2 is deployed).
   (Out of this repo.)
4. **Follow-ups:** H1 outcome tie-in on episodes; generalize `exclude_categories` to the
   cross-service scoping H2 originally described.

## Acceptance criteria

- [ ] An episode is recordable as a tagged fact (`category="episodic"`) with `meta.episode`
      and optional `derived_from` edges — no new schema.
- [ ] **Episodes are stored whole and immutable:** a multi-sentence decision+rationale is one
      row (no atomization); two episodes on the same topic both persist (no dedup/merge); a later
      episode never supersedes/flags an earlier one (no contradiction; never `invalid_at`).
- [ ] **A semantic write never merges-with or contradiction-flags an episode** (episodes excluded
      from write-time recall).
- [ ] **Reserved-tag integrity:** a non-episode write cannot land with `category="episodic"`.
- [ ] **The producer is reachable from MCP** (a `praxis_record_episode` tool, or `category`+`meta`
      honored on `praxis_add_insight`) — the harness can actually write an episode.
- [ ] Episodes remain retrievable on their own via the `category="episodic"` query, including
      after `stale_derived()` flags one (a stale episode is still findable).
- [ ] No regression: semantic retrieval of non-episodic facts is unchanged; existing
      eval/unit suites stay green.
- [ ] No new table/column/migration.
- [ ] **H2:** `search(exclude_categories=[…])` omits those categories on both the cosine and
      keyword branches; equality `filters`/`scope` behavior is unchanged.
- [ ] **H2:** `/context` (+ `praxis_get_context`) excludes `"episodic"` by default;
      `include_episodic=true` brings them back; the H2 red-spec flips RED → GREEN with no
      client-side drop.

## Open questions (resolved)

- **Reserved value — RESOLVED: `"episodic"`.** Matches the taxonomy. Make it a named constant,
  reserve it, confirm no existing category emits it, and enforce reserved-tag integrity (§1c).
- **Helper home / `drop_episodic` — RESOLVED: don't build it if H2 ships in the same release**
  (§3). It's vestigial against H2's server-side exclude. Only build it (as a tiny shared util) if
  H4 lands before H2.
- **`record_episode` surface — RESOLVED: must reach MCP now** (§2), not graph-only. Graph-only
  leaves H4 with no live producer for its stated writer (the harness, which uses MCP). Expose
  `praxis_record_episode` or honor `category`+`meta` on `praxis_add_insight`.
- **H2 default scope — RESOLVED: per-lane, not "internal sees everything"** (Part 2.4). Default
  the exclude at the `/context` route AND apply it to the internal recall of *semantic* writes
  (so new facts don't clash with episodes); episode writes run no recall (store-only lane).
  Mounted overlays are covered by applying the exclude to the unioned result.

## Remaining open question

- **`stale_derived()` × episodic:** when a basis fact is invalidated, the dependent episode is
  flagged stale (good). Confirm a stale episode surfaces only on the episode-log/stale query and
  not back into `/context` (it shouldn't, since `/context` excludes `"episodic"` regardless of
  stale state) — and decide whether a stale episode's `meta.episode.outcome` should auto-note the
  basis change. (Lean: surface on the stale query only; leave `outcome` to the H1 tie-in.)
