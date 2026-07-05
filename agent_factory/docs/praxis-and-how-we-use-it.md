# Praxis: What It Is and How the Agent Factory Uses It

> Grounding document. Part 1 describes Praxis as it exists today (tracked against the
> source at `../praxis`, including the knowledge-graph capabilities that recently landed:
> mountable read-only snapshot overlays, bi-temporal validity, hybrid retrieval, a
> two-stage contradiction engine with opt-in auto-resolution, and a full MCP tool
> surface). Everything in Part 1 is ground truth we build on; everything in Part 2 is our
> design intent and is open to iteration.

---

## Part 1 — What Praxis Is

Praxis is a **knowledge graph exposed over HTTP (and MCP)**. You give it documents or
single facts; it distills, deduplicates, merges, and stores them as atomic facts; you ask
it a natural-language question and it returns the most relevant currently-valid facts with
provenance. It does the knowledge-shaped work — distillation, dedup, merge, contradiction
detection + resolution, similarity *and* keyword retrieval, temporal validity — so a
consumer only has to authenticate, write, ask, and audit.

### 1.1 How to connect

Two ways in, same backend and same tenancy rules:

**HTTP.** Every request carries two headers:
- `X-Praxis-Key: pxk_...` — a scoped API key
- `X-Praxis-Org: <org_id>` — must equal the key's org (else 403)

Backends:
- **Local:** `http://localhost:8000` (run in the praxis repo: `uv run python -m knowledge.serve`)
- **Prod:** `https://bdsikf2bc8.us-east-1.awsapprunner.com` (App Runner, postgres-backed)

`GET /health` is unauthenticated and returns `{"status":"ok","store":"postgres"}`.

A consumer SDK lives at `../praxis/praxis_client/`. It is dependency-light (uses `httpx`
if present, else stdlib `urllib`) and copyable standalone. Core methods:
`get_context(query, top_k)`, `context_text(...)`, `ingest(text, source, state)`,
`ingest_batch(documents, state)`, `add_insight(insight, scope, category, source)`.
It reads no env vars itself — config is passed to the constructor.

**MCP.** Praxis also ships an MCP server (`../praxis/knowledge/mcp`) that exposes the same
operations as tools, so a coding agent can use Praxis directly without going through our
HTTP port: `praxis_get_context`, `praxis_add_insight` / `praxis_ingest`,
`praxis_list_graph` / `praxis_insert_fact` / `praxis_edit_fact`, fact lifecycle
(`praxis_promote_fact` / `praxis_reject_fact` / `praxis_delete_fact`), contradictions
(`praxis_get_contradictions` / `praxis_resolve_contradiction`), snapshots
(`praxis_save_snapshot` / `praxis_load_snapshot` / `praxis_list_snapshots` /
`praxis_delete_snapshot` / `praxis_clear_graph`), read-only overlays
(`praxis_list_mounts` / `praxis_mount_snapshot` / `praxis_unmount_snapshot`), and
cross-member sharing (`praxis_list_org_sources` / `praxis_browse_snapshot` /
`praxis_fold_in`), plus login/org tools. The MCP tools are thin authenticated wrappers
over the HTTP endpoints below and inherit the exact same `(org_id, user_id)` tenancy — so
"HTTP vs MCP" is only an ergonomics choice, never a capability or isolation difference.

### 1.2 The endpoints that matter to us

| Endpoint | What it does |
|---|---|
| `GET /health` | Liveness, no auth. |
| `GET /me` | Who the key maps to: `{sub, email, orgs[]}`. Confirms which (org, principal) you're operating as. |
| `POST /ingest` | `{documents:[{text,source}], state}` → server-side LLM **distillation** into atomic facts, then dedup/merge/contradiction reconciliation. Slow/async. |
| `POST /insights` | `{insight, scope, category, source}` → add **one already-atomic, pre-approved fact**. No distillation; still runs dedup/merge/contradiction. |
| `GET /context?query=&top_k=&as_of=` | Returns `{context, hits:[{id,text,score,source,scope,category,mounted,owner,snapshot}]}` — currently-valid active facts, **hybrid-ranked (semantic + keyword)**, with provenance. Optional `as_of` for point-in-time recall; includes mounted overlay facts flagged `mounted`/`owner`. |
| `GET /candidates?state=active\|proposed\|rejected\|all` | Audit what's stored, including anything dropped or superseded. |
| `GET /graph?state=` | `{graph:{nodes,edges}}` — the fact graph with typed edges (`contradiction`, `contradicted_by`, `supersedes`). |
| `GET /contradictions` + `POST /contradictions/{id}/resolve` | Inspect and resolve conflicting facts (keep-one or custom text). |
| `GET/POST/DELETE /mounts` | List / mount / unmount **read-only snapshot overlays** that are read at query time without being merged into the live graph. |
| `POST /snapshots`, `/snapshots/{name}/load`, `DELETE /snapshots/{name}` | Save / restore / delete a full-graph checkpoint. |
| `GET /org/sources`, `/org/sources/{user}/snapshots/{name}/facts`, `POST /fold-in` | Browse and copy another org member's snapshot facts into your graph (deduped, conflicts flagged). |
| `POST /orgs`, `/orgs/join` · `POST /apikeys` | Org and key management. |

### 1.3 The facts about Praxis that shape our design

These are the non-obvious, verified behaviors. Our architecture lives or dies by them.

1. **Tenancy is the base queryable partition.** Scoping is `(org_id, user_id)`. The read
   predicate is `WHERE org_id = ? AND (shared = true OR user_id = ?)`. So a principal sees
   its own facts **plus** org-wide `shared` facts. Facts written under one principal are
   invisible to another unless marked shared. An API key resolves to one principal; check
   `GET /me` to confirm which.

2. **Snapshots are checkpoint/rollback AND mountable read-only overlays.** A snapshot is a
   full-graph save (`cached_facts`), restorable with `mode=replace` (truncate + reinsert)
   or `mode=add` (merge). On top of that, a principal can **mount** a snapshot — its own or
   any org member's — as a read-only overlay: `/context` then unions the live graph **plus**
   the mounted snapshots, with mounted hits flagged `mounted`/`owner`. Crucially, a mounted
   overlay is **not merged into the live graph** and is **not carried over when you save a
   snapshot** — it is pure read-time composition (a single `UNION ALL` over the indexed live
   and cached tables, query embedded once). So you get read-time knowledge composition
   without polluting a principal's live graph or its checkpoints. Mounting is editable from
   both the HTTP API and MCP.

3. **`/context` returns currently-valid active facts, hybrid-ranked.** Retrieval fuses
   semantic (pgvector cosine) **and** keyword (BM25/full-text) ranking via Reciprocal Rank
   Fusion, so exact terms — symbol names, error codes, file paths, identifiers — surface
   reliably, not just paraphrase-similar text. Results are token-bounded (~8KB), each hit
   carries provenance (`source`, `score`, `scope`, `category`), and proposed/rejected facts
   never appear. Retrieval respects **bitemporal validity**: only facts valid *now* are
   returned by default, and an optional `as_of` timestamp gives point-in-time recall ("what
   did we believe was true as of plan-time?"). Mounted overlays are included and flagged.

4. **Ingestion distills, then reconciles — loss is reduced but not zero.** `/ingest` runs an
   LLM distillation per document (needs the backend's model + postgres), taking minutes for
   batches; facts keep appearing for seconds after the call returns. Distillation
   **explodes** one document into many atomic facts. The write pipeline then reconciles each
   one: exact/semantic **dedup**, a Mem0-style **merge/augment** step that folds a
   related-but-additive fact into an existing one (e.g. "likes cheese pizza" + "likes
   chicken pizza" → one merged fact) instead of dropping or duplicating it, and contradiction
   detection. This meaningfully cuts the old "silent near-duplicate drop" problem — but
   distillation can still under-emit on highly templated/tabular input (rows that share a
   sentence shape), so **bulk/tabular ingest still warrants a rejected-pile audit**.

5. **The contradiction engine is two-stage, with invalidate-and-keep and opt-in
   auto-resolution.** Conflicts are detected at write time by (a) **structural** claim-slot
   comparison — atomic `(subject, attribute, value)` claims, deterministic for numeric/stance
   clashes — and (b) a **semantic** fallback that catches paraphrase contradictions with no
   shared slot ("loves working outdoors" vs "can't stand being outside") via embedding-narrow
   + LLM judgment, precision-first. On conflict the loser is **invalidated and kept** (state
   → `rejected`, `invalid_at` set to when the winner became valid, text + edges preserved) —
   never silently dropped — so the full history stays queryable via `as_of`. High-confidence
   conflicts can **auto-resolve** (opt-in); ambiguous ones surface in `/contradictions` for a
   keep-one or custom-text decision. This is a real truth-maintenance layer to lean on, not
   rebuild.

### 1.4 Write paths, compared

| | `/ingest` | `/insights` |
|---|---|---|
| Input | Unstructured document(s) | One atomic fact |
| Processing | LLM distillation → many facts → dedup/merge/contradiction | Direct insert → redact/dedup/merge/contradiction |
| Speed | Slow / async (minutes) | Fast / synchronous |
| Loss risk | Lower than before (merge path), still real on tabular input | Low |
| Use for | Raw source material we haven't pre-digested | Facts we already know and have shaped |

### 1.5 Fields on a fact

- `scope` — free-text grouping (e.g. `global`, or a project tag). Dashboards group by it;
  `global`/absent scope is treated as broader. No server-side enum.
- `category` — free-text aspect label, used in recall/matching. No server-side enum.
- `state` — `active` (retrievable) · `proposed` · `rejected`.
- `valid_at` / `invalid_at` — **world-time validity** (bitemporal). `invalid_at = NULL` means
  currently valid; a superseded fact gets `invalid_at` set rather than being deleted.
  `as_of` on `/context` filters on this window for point-in-time recall.
- `provenance` — `source`/`score` returned on every hit; use it to cite what grounded a decision.

### 1.6 Operational cautions

- **Identity must match.** Mint the key so its principal matches the identity you'll browse,
  mount, and write with; cross-identity facts are invisible unless shared or mounted. Verify
  with `GET /me`.
- **Use long timeouts** (120–180s) on ingest.
- **Audit bulk/tabular writes.** The merge path reduces silent loss, but after any bulk or
  tabular ingest still check `/candidates?state=rejected` and `?state=all` before trusting the
  active set.
- **Plan a fallback.** App Runner cold starts can be slow and Praxis may be unreachable; keep
  a local copy of critical knowledge and label which source answered.

---

## Part 2 — How the Agent Factory Uses Praxis

> Design intent. This is the part we iterate on. The goal is to lean on Praxis as the
> factory's memory **as heavily as possible**, letting all knowledge-shaped work live in
> Praxis while the factory only authenticates, writes, asks, and audits.

### 2.1 The role Praxis plays

Praxis is the factory's **single source of durable knowledge** — the memory that makes the
factory compound across projects. The factory's own code is deliberately thin: an
orchestrator, a knowledge port, and execution agents. The "smart" parts (what's relevant,
what contradicts what, what's a duplicate, what's still true) are Praxis's job, not ours.

### 2.2 The partition model: tenancy for isolation, mounts for composition

Isolation is still derived from **tenancy**, not snapshots. Two layers are queryable in one
call:

- **General pool** — reusable conventions, learnings, and rules that should benefit every
  project. Written as `shared = true` under a stable org.
- **Project pool** — the requirements and in-flight learnings for one specific project (e.g.
  the team mental-performance app). Written under a **per-project principal** (distinct
  `user_id`), `shared = false`.

A query made with a project's key returns *its own project facts + the shared general rules*
in a single `/context` call, and provenance lets us tell them apart. A new project is a new
key over the same shared base. This is the isolation the project needs — derived from
tenancy.

**Mounts add a second, finer tool: read-time composition.** Beyond the blunt `shared` flag,
a project principal can **mount** specific snapshots as read-only overlays — e.g. a frozen
"golden conventions" pack, a vetted library-knowledge snapshot, or a sibling project's
snapshot — so its retrieval is enriched by that knowledge **without merging it into the
project's live graph and without it leaking into the project's own checkpoints**. Mounts are
reversible (unmount and it's gone from reads) and selective (mount exactly the packs a task
needs). This gives the factory a clean way to grant a project read access to curated or
cross-project knowledge that isn't (or shouldn't be) globally `shared`. *(This supersedes the
earlier "snapshots are checkpoint-only" stance: snapshots are now both checkpoints and a
composition primitive.)*

**Snapshots** still serve checkpoint/rollback — a save of the general pool before a risky
bulk ingest or learning-write phase, so we can roll back if the graph gets polluted — and now
double as the unit we mount.

#### Worked pattern: iterate on general ideas, read a PRD alongside it

The deciding rule is **load vs. mount**, and it turns on *which graph you're writing to*:

- **Load** (`POST /snapshots/{name}/load`) pulls a snapshot **into** the live graph
  (`mode=replace` truncates and reinserts; `mode=add` merges). The snapshot's facts become
  live facts — editable, but now part of this principal's graph **and part of its next save**.
- **Mount** (`POST /mounts`) exposes a snapshot as a **read-only overlay**: its facts show up
  in `/context` (flagged `mounted`), but they are **not** in the live graph and are **not**
  carried over when you save a snapshot.

So for "I'm iterating on a knowledge graph of general coding ideas and want to read a PRD
while I work," the live graph is the **general coding ideas** (the thing you keep writing to),
and the PRD is **mounted**, not loaded:

1. Save/keep the PRD as its own snapshot (e.g. `praxis_save_snapshot("prd-mental-perf")`,
   typically captured under a project principal or after ingesting the PRD into a scratch
   graph and snapshotting it).
2. With the general-ideas principal active, **mount** it:
   `praxis_mount_snapshot("prd-mental-perf", source_user=<prd owner>)` (omit `source_user`
   to mount your own snapshot).
3. Iterate. Every `/context` call now recalls general coding ideas **and** PRD facts in one
   ranked result; new facts/learnings you write land **only** in the general-ideas graph. The
   PRD is read-only context — you cannot accidentally edit it through the overlay, and it
   cannot drift.
4. Save the general-ideas graph whenever you like — the snapshot contains **only** your
   general ideas, never the mounted PRD. The two stay cleanly separated.
5. **Unmount** when the PRD is no longer relevant
   (`praxis_unmount_snapshot("prd-mental-perf")`) and reads revert to general ideas alone.

If instead you wanted the PRD to *become* part of the graph you're editing (rare for this
case — it's how you'd seed a fresh project pool), you would **load** it, accepting that it now
lives in that graph and in its saves. For "read while iterating," always mount.

### 2.3 The two interaction modes

**Plan-building (write-heavy + audit).** Bring a project's requirements into its project
pool. The merge/augment path reduces silent near-duplicate loss, but the source PRD is
tabular-heavy, so this phase still:
1. shapes requirements into atomic, lexically distinct facts (not raw table rows),
2. writes them (prefer `/insights` for facts we've already shaped; `/ingest` only for genuinely raw text),
3. **audits `/candidates?state=rejected`** and confirms the active set is complete before trusting it.

Snapshot the project pool at the end of plan-building, so execution can checkpoint against a
known-good plan state — and so other projects could later mount it.

**Execution (read-heavy + targeted writes).** Build the target app. For each task, retrieve
grounding context (project + general, plus any mounted knowledge packs) via `/context`, act,
verify, and write confirmed learnings/fixes back — gated through the contradiction engine so
we don't poison the pool. Because retrieval is hybrid, tasks can recall by exact symbol/error
string, not just fuzzy similarity. Because contradictions invalidate-and-keep with world-time
validity, a superseded decision (we switched libraries, we changed the schema) is recorded as
a *temporal* change, and `as_of` lets a task reconstruct what the plan believed at any point.
Generalizable learnings get promoted into the shared general pool over time.

### 2.4 Write-path policy

- Facts we already know and have shaped (a confirmed fix, a chosen library, a resolved
  decision) → `/insights`. Fast, low-loss.
- Genuinely unstructured source material we haven't digested → `/ingest`, followed by a
  rejected-pile audit on tabular/bulk input.
- Lean on the **merge/augment** path for related-additive learnings instead of writing
  near-duplicates; let the engine fold them together.
- Enable **opt-in auto-resolution** for high-confidence contradictions during write-back so
  the pool self-corrects without a human in the loop; reserve the manual `/contradictions`
  queue for genuinely ambiguous conflicts.
- Never block an execution step on an ingest completing (it's async/minutes).

### 2.5 What we deliberately do NOT build (because Praxis already does it)

- Similarity *and* keyword retrieval / ranking → hybrid `/context`.
- Deduplication and additive merge of facts → distillation + dedup/merge step.
- Contradiction detection (structural + semantic) and resolution → the contradiction engine.
- Temporal validity / point-in-time recall → bitemporal `valid_at`/`invalid_at` + `as_of`.
- Read-time knowledge composition → mounted snapshot overlays.
- Provenance tracking → returned on every hit.

### 2.6 What the factory still has to provide

- **A knowledge port** — one narrow internal module wrapping all Praxis access, encoding the
  routing rules above (which endpoint, which scope, what to mount, when to use `as_of`, what
  to audit), so the quirks live in one place and the rest of the factory codes against a clean
  contract.
- **Ingestion integrity** — the table-linearization + rejected-pile audit that keeps the
  tabular PRD from silently losing its most important content (the merge path helps, but
  doesn't replace the audit).
- **A local fallback** — a cached copy of critical knowledge for when Praxis is cold or
  unreachable, labeled by which source answered.
- **The orchestration and execution logic** — how tasks are decomposed, what context (and
  which mounted packs) each task is given, how output is verified, and what gets written back.

### 2.7 Open questions (to resolve as we design Part 3)

- Do we keep the explicit two-phase (plan / execute) split, or collapse it into one loop where
  a fact's `(user_id, shared)` *is* its lifecycle stage?
- When does cross-project knowledge get **mounted** (read-only, reversible) vs **promoted to
  `shared`** (permanent, global)? Mounts now make this a real, gradable decision.
- How hermetic should each task's knowledge inputs be — declared up front (a fixed set of
  mounts + a query) or queried ad hoc?
- What is the promotion gate from project pool → shared general pool?
- How do verification outcomes feed back into fact trust (state, `invalid_at`,
  auto-resolution confidence) so the pool self-cleans?
