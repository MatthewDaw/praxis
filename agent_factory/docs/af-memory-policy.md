# Praxis knowledge-port policy

This is the factory's **internal reference** for all Praxis knowledge-graph access — the single
"knowledge port" as policy. It is **not** an invocable skill; it is the document the `af-*` skills
(`af-plan`, `af-intake-plan`, `af-build`, `af-wireframe`) and the plugin hooks cite whenever they read
from or write to Praxis. It encodes which MCP tool to use, the tabular ingestion-integrity audit,
tenancy via snapshots/mounts, the ticket/check state model, and the write-back rules.

## How work flows (this factory's methodology — read first)

State lives in ONE place: Praxis. There are no JSON status files, no locks on disk, no self-set "done"
flags. A ticket (requirement) and a check are Praxis facts; everything about what is built/claimed/passed
is state ON THE TICKET'S Praxis node. To do ANY unit of work you follow exactly this loop:

1. FIND   — query Praxis for the next incomplete ticket in scope (incomplete = never-built | regressed |
            stale, derived from recorded outcomes). Pass the BARE project name (e.g. "team-app"); the
            endpoint adds the "prd-" prefix itself — passing "prd-team-app" returns EMPTY and silently
            hides all work.
2. CLAIM  — atomically set the ticket's meta.build_state="in_progress" with claim_owner=you + a heartbeat.
            The claim is a LEASE: refresh the heartbeat while working; a stale lease auto-reclaims so a
            dead agent never strands a ticket. Parallel agents never double-work because a live claim is
            visible to all.
3. RESOLVE— determine which checks this ticket must pass by QUERY (its tag ∪ its surfaces ∪ semantic
            match against active checks). The ticket NEVER stores its own check list. Truncate any prior
            per-check state, then PIN the freshly-resolved set onto the ticket as this pass's contract.
4. BUILD  — do the work to satisfy the ticket's acceptance condition.
5. VERIFY — run each pinned check; record each pass ON THE TICKET NODE (never on the check — checks are
            read-only during builds). External signals only; never self-judge.
6. FINISH — only when EVERY pinned check passed: record a succeeded outcome and release the lease
            (build_state="finished"). If any check fails, record a failed outcome — that regresses the
            ticket so it re-enters the FIND set and is re-done.

Praxis is a HARD dependency: if it is unreachable the factory STOPS (the gate blocks) — it never proceeds
on a guess. The single Stop gate (build_completeness) enforces this loop: it blocks the turn from ending
while you hold an unfinished claim or scoped incomplete tickets remain.

This policy is the knowledge-port: it defines HOW every step above touches Praxis. It owns the canonical
meta keys and lifecycle verbs the plugin uses to FIND/CLAIM/RESOLVE/VERIFY/FINISH (§1), the fail-closed
Praxis dependency, and the durable-knowledge write-back rules (§2–§7) for the learnings the loop produces.

# Factory Memory Policy

**Praxis is the single source of dynamic truth.** It stores tickets (requirements), checks,
and the outcomes/state that say what is built and what passed. Code lives in git; static
configuration lives in JSON. Everything *about what is built / claimed / passed* lives in
Praxis and nowhere else.

This document is the one place that decides *how* the factory touches memory. The `af-*` skills and
the plugin hooks follow these rules; they do not invent ad-hoc `praxis_*` conventions.

Two distinct surfaces touch Praxis, and they must not be confused:

- **Dynamic build/validation state** (ticket lifecycle, claims/leases, pinned checks,
  passes). This is written *only* by deterministic plugin code through
  `hooks/_praxis.py` + `hooks/_ticket_state.py` (the contract lives in
  `docs/factory-state-contract.md`). Skills/agents do **not** hand-author `build_state`,
  claims, pins, or passes — they call the API. See §1.
- **Durable knowledge** (learnings, decisions, requirements, grounding facts). This is
  written through the `praxis_*` MCP tools per §3–§7.

## Fail-closed: Praxis is a HARD dependency

Praxis is not optional. If it is unreachable, unauthenticated, or errors, the factory
**crashes and stops** — it never proceeds on stale or assumed state.

- The plugin client raises `PraxisUnreachable` on any connection/HTTP/auth failure.
- A Stop-gate that catches `PraxisUnreachable` **BLOCKS**. A gate that cannot prove the
  truth never lets work pass (fail-CLOSED, never fail-open).
- There is **no local fallback store**. No `.factory/*.json` manifest, no event-log mirror,
  no "ephemeral run state" file substitutes for Praxis. JSON is **static config only** and
  is NEVER written or edited as work completes, checks run, or things validate. Any
  `json.dump` of build/validation/review/audit/preflight/wireframe state is a bug.

## 0. Always confirm tenancy first

The factory operates **only** in the `agent-factory` org. Run `praxis_whoami` **before any
MCP memory operation, and again after every `/mcp` reconnect or Praxis restart** — not just
once per session. If the active org is not `agent-factory`, run
`praxis_select_org("agent-factory")`. Never write into another org. (The plugin hook client
reads the org from `PRAXIS_ORG`, default `agent-factory`, sent as the `x-praxis-org` header.)

> **Why "after every reconnect" (learned live 2026-06-25):** a restart can silently reset the
> active org to a default (e.g. `praxis`), and if multiple agents on the same machine **share one
> Praxis identity cache** (`~/.praxis/mcp.json`), another agent's login will clobber *your* active
> org mid-session — your writes then land in the wrong org. Two defenses, both required: (1) this
> agent pins its **own** cache via `PRAXIS_MCP_CACHE` in the MCP server's `env` (so co-tenant
> sessions can't collide); (2) re-`whoami` after every reconnect and re-select the org before writing.

- **Durable knowledge = org-shared snapshots; working memory = per-user scratch.** The tenancy model
  is `org → space → snapshot` plus a private **working memory** (the live scratch graph, keyed to
  your authenticated principal — no space, no snapshot ever appears on it). A **space** is an
  org-shared "project folder"; a **snapshot** is a saved, org-shared named graph inside a space,
  addressed by `(space, snapshot)` and readable by any org member. `save_snapshot(space, snapshot)`
  captures the *whole* working graph into that snapshot; the working graph is only the current
  session's set. Compose reference knowledge with read-only **`mount(space, snapshot)`** (overlays a
  snapshot onto your working-memory reads without merging into it or its saves), never by keeping it
  live.
- **`general-pool`** — the durable cross-project conventions + learnings (incl. planning
  conventions and the ambiguity-example library), kept as a snapshot in the shared-conventions space
  and mounted read-only by plan and execution alike.
- **Project space** — each project corresponds to exactly ONE **space** whose id is the BARE project
  name (e.g. `team-app`). It holds the project's snapshots: `prd-<project>` (plan + tickets, built
  during plan-hardening) plus the per-project check snapshots (§1). Mount `(<project>, prd-<project>)`
  read-only during execution.
- Projects are partitioned by **space**, not by user_id — spaces + snapshots are the org-shared
  partition primitive; working memory is the per-user private lane. **`mount(space, snapshot)`** =
  read-only compose; **`load(space, snapshot)`** = copy a snapshot INTO your working memory (only to
  edit it, then `save_snapshot` back).

## 1. Dynamic build/validation state — the ticket/check model

This is the heart of the new model. All of it lives on Praxis fact nodes and is read/written
*live* by `hooks/_ticket_state.py` (which calls `hooks/_praxis.py`). Skills orchestrate; they
do not write these meta keys by hand. The canonical reference is `docs/factory-state-contract.md`.

### Canonical meta keys (on the requirement / ticket node)

| Key                  | Type                                     | Meaning                                                          |
|----------------------|------------------------------------------|------------------------------------------------------------------|
| `build_state`        | `"incomplete"｜"in_progress"｜"finished"` | The ticket's lifecycle state. Absent ≡ `incomplete`.            |
| `claim_owner`        | `str`                                    | Session/agent id holding the lease.                              |
| `claim_at`           | `float` (epoch seconds)                  | When this owner first claimed.                                   |
| `claim_heartbeat_at` | `float` (epoch seconds)                  | Last liveness bump.                                              |
| `claim_lease_ttl`    | `int` (seconds)                          | Lease is STALE when `now - claim_heartbeat_at > claim_lease_ttl`.|
| `pinned_checks`      | `list[{check_id, passed, ran_at}]`       | THIS pass's completion contract (the resolved set — see below).  |

A `pinned_checks` entry is `{ "check_id": str, "passed": bool｜null, "ran_at": float｜null }`
(`null` = not yet run). The ticket carries identity (tags, surfaces, semantics) but **never** an
authored list of its checks — that is always a fresh query (below).

### Per-ticket lifecycle

A ticket on the requirement node moves `incomplete → in_progress → finished`. On **start**
(`start_ticket(cid, owner, project)` does all three atomically):

1. **claim** — `incomplete → in_progress`, stamping `claim_owner` + `claim_at` +
   `claim_heartbeat_at` + `claim_lease_ttl`.
2. **resolve checks** — run the applicability QUERY (below) against the *active* checks.
3. **pin** — `pin_requirements` **TRUNCATES** any prior `pinned_checks` and writes the FRESH resolved
   set as this pass's completion contract.

Then **build + validate**: run each pinned check and record each pass **ON THE TICKET NODE** via
`record_validation_pass` (never on the check fact). `heartbeat` periodically to keep the lease live.

The ticket is **finished IFF** `all_validations_passed` — at least one pinned check, and every pinned
check passed — then `release(cid, owner, state="finished")`. Yielding cleanly without finishing →
`release(cid, owner, state="incomplete")`.

### Claiming is a LEASE, not a lock

"A build run is active" ≡ *this session owns a live, unfinished `in_progress` claim*, read from
Praxis — **not** a local file flag. A **stale** lease (`now - claim_heartbeat_at > claim_lease_ttl`,
default `DEFAULT_LEASE_TTL_S = 900`) is auto-reclaimable, so nothing ever dangles if an agent dies.

Claiming is **race-tolerant (v1)**: read-modify-write via `patch_meta` (PATCH `/candidates/{cid}`,
which MERGES meta). No server-side CAS is assumed — two agents can both claim a free/stale ticket,
a rare and **harmless** double-claim (idempotent wasted work), not corruption. Because `patch_meta`
merges and cannot delete keys, `release` NULLs the lease keys rather than removing them; null
heartbeat/ttl reads as not-live.

### Which checks apply = a QUERY (resolved fresh at ticket start)

`resolve_validation_requirements(ticket, project)` returns the **MANDATORY (precise)** coverage contract — the
de-duplicated union of three lanes:

- **tag match** — active `category="check"` facts whose `meta.applies_to` matches any of the ticket's
  tags (`meta.tags` / `meta.applies_to`); via `facts_by`.
- **`"*"` wildcard** — universal gates (typecheck/build/lint/test) that apply to EVERY ticket, via an
  explicit `facts_by(meta={"applies_to": "*"})`. Separate because a per-tag query can't surface a `["*"]`
  check (membership matches the stored value; a ticket's tags never include `"*"`) — omit it and the
  baseline floor silently fails to resolve.
- **surface match** — active checks bound (via the `renders` edge) to any surface the ticket renders
  (`meta.surfaces` / `meta.screen_ids`); via `surface_checks` → `/surfaces/{screen}/checks`. A UI check is
  surface-bound (or UI-tagged) so it resolves ONLY onto screen-rendering tickets, never a backend ticket.

**The semantic lane is ADVISORY** — `retrieve_advisory_checks(ticket, project, scope, checks_ref,
top_k)` runs a hybrid `/context` retrieval of `category="check"` facts near the ticket text and returns
them as **candidate inspiration** for synthesis. They are NEVER pinned/required and NEVER gate completion:
the worker folds in the relevant ones and ignores the rest, so an irrelevant retrieval is harmless. The
hard guarantee stays on the precise mandatory set; semantics only boosts recall. Checks are **declarative
+ read-only during builds** — a check owns its own applicability predicate and is edited only on explicit
user request, never as a side effect of building.

**The checks-space seam — checks are RESOLVED from a dedicated per-project snapshot.** Check
resolution reads from a snapshot **separate** from the `prd-<project>` graph that holds the tickets
and their state, so validation rules live on their own. Both live in the SAME project space
(`space=<project>`, the bare project name); only the *snapshot* differs.
`resolve_validation_requirements(..., scope=...)` defaults the read snapshot by scope:
`scope="validation"` (af-build per-ticket) → **`building-validation`** (renamed from
`coding-validation`); `scope="planning"` (af-intake-plan whole-plan) → **`planning-validation`**;
`scope=None` (back-compat) → the ticket/default reference. Only the *check reads* honor it (via a
per-request `x-praxis-space` + `x-praxis-snapshot` override in `facts_by` / `surface_checks`); ticket
state (claims, pins, passes) is untouched — it stays on the `prd-<project>` snapshot. Callers override
per-run with `checks_ref=(space, snapshot)` (the `af-build` / `af-intake-plan` slash argument,
`--checks-space`); `checks_ref=None` forces the ticket/default reference. A check is only resolvable
if it was written INTO the snapshot RESOLVE reads — amend-mode writes must target
`space=<project>, snapshot=building-validation` / `planning-validation` accordingly.

### The plugin API (the only writer of build state)

`hooks/_praxis.py` (stdlib-only client; every method raises `PraxisUnreachable` on failure):

```python
incomplete_requirements(project, *, exclude_leased=False, space=None, snapshot=None) -> list[dict]  # prd-<project> tickets
get_fact(cid, *, space=None, snapshot=None) -> dict
facts_by(category=None, meta=None, state="active", space=None, snapshot=None) -> list[dict]  # (space,snapshot)= checks-snapshot override
patch_meta(cid, meta_dict, *, space=None, snapshot=None) -> dict   # MERGE meta (build_state / claim / pinned_checks)
record_outcome(cid, success, *, space=None, snapshot=None) -> dict
surface_checks(project, screen_id, scope=None, space=None, snapshot=None) -> list[dict]  # (space,snapshot)= checks-snapshot override
context(query, *, top_k=10, as_of=None, space=None, snapshot=None) -> list[dict]  # hybrid retrieval (the semantic lane)
ping() -> bool
```

`hooks/_ticket_state.py` (the lifecycle verbs; `ticket` args accept a fact id or a fetched dict):

```python
resolve_validation_requirements(ticket, project="", scope=None, checks_ref=<default>) -> list[dict]   # MANDATORY: tag ∪ "*" ∪ surface; checks_ref = the (space,snapshot) seam
retrieve_advisory_checks(ticket, project="", scope=None, checks_ref=<default>, top_k=10) -> list[dict]  # ADVISORY semantic lane (inspiration; never gates)
pin_requirements(cid, requirements) -> dict                       # truncate + pin fresh contract
record_validation_pass(cid, validation_id, passed, ran_at=None) -> dict   # records ON THE TICKET NODE
all_validations_passed(ticket) -> bool                            # ≥1 pinned AND all passed

claim(cid, owner, ttl=900) -> bool                                 # incomplete -> in_progress
heartbeat(cid, owner) -> bool                                      # bump iff still holding live lease
release(cid, owner, state) -> bool                                 # state in {"finished","incomplete"}

start_ticket(cid, owner, project="", ttl=900, checks_ref=<default>) -> list[dict]|None  # claim + resolve (space=project, snapshot from checks_ref) + pin
```

### One completeness gate (no multi-gate machinery)

There is exactly **one** completeness gate. It enforces, live against Praxis, "are there
incomplete tickets/checks for this scope?" Everything else that used to be a separate gate is now a
ticket or a check: a review/audit finding becomes a Praxis ticket/check; a missing env dependency
becomes a *failing check*; an unrendered wireframe surface becomes an *incomplete requirement*. The
only residue kept from a review/audit panel is a tiny "panel-ran" Praxis **episode** assertion (so
the act of reviewing cannot be silently skipped) — recorded with `praxis_record_episode`, **not** a
findings state machine and **not** a local manifest.

## 2. Choose the write path (durable knowledge)

| Input | Tool | Why |
|---|---|---|
| A shaped, already-true atomic fact (a chosen library, a confirmed fix, a convention) | `praxis_add_insight` | Fast, synchronous, low-loss; runs dedup/merge/contradiction. |
| Raw unstructured prose we have not digested | `praxis_ingest` | Server-side distillation. Slow/async — keep it off the critical path. |
| **Tabular / templated input** (tables, row-per-field specs, `key: value` blocks) | **Linearize first, then `praxis_add_insight` per row** | Distillation silently under-emits on tables (gap H6). **Never raw-`ingest` a table.** |
| A raw record that must bypass the pipeline for review | `praxis_insert_fact` | Lands in `proposed`; special cases only. |

Prefer shaping facts and using `add_insight` over `ingest` wherever practical — it avoids
both the latency and the distillation loss. For **several** facts at once, use
**`praxis_add_insights`** (bulk) — one round-trip, written serially server-side, with a per-item
`retrievable` flag confirming read-your-writes (this is the right tool for a batch, not a loop of
single calls). **Cap the batch (~3–5 LLM-heavy items).** Each item runs distillation + a
conflict-judge LLM call, so a larger batch can exceed the ~120s client timeout and **partially
commit** (learned live: a 3-item bulk timed out with only 2 items landed). The read-back rule below
applies to bulk too — on a bulk timeout, read back the per-item results / `list_graph` and re-add
only the items that didn't land.

**Stamp metadata on every write** (all honored + returned — H12): `source` (where it came from, for
provenance citation), `category` (`"requirement"` / `"check"` / `"learning"` / etc.), `meta`
(structured — e.g. a check's `applies_to` tags or a requirement's `tags`/`surfaces`), and
`derived_from=[ids]` (the facts a learning was built on — H5, so an invalidated basis later surfaces
this fact as suspect via `praxis_get_stale_derivations`).

**Conflict mode (`on_conflict`) — choose deliberately:**
- `on_conflict="surface"` — a detected contradiction is **surfaced, not resolved**: both facts
  kept (incumbent `active`, newcomer `proposed`, neither rejected), a pending pair appears in
  `praxis_get_contradictions` with a `pair_id`, settled by `praxis_resolve_contradiction`. **Use
  this whenever a human should adjudicate** — all of plan-hardening, and any write where losing a
  fact silently would be wrong.
- `on_conflict="auto_resolve"` (the default) — newest wins, loser silently → `rejected`, nothing
  flagged. Only use when you *intend* a confirmed overwrite (e.g. superseding a known-stale learning).

**Write serially — never fire parallel write bursts.** Conflict-checked writes are expensive
(inline semantic-judge LLM call) and concurrent bursts have driven the backend to 500 on all
requests (gap H13.2). Issue `add_insight`/`ingest` **one at a time**, awaiting each, even when you
have many facts to write. Throughput is not worth a poisoned backend.

**Write timeouts are false negatives — read back, then re-add only if absent.** A conflict-checked
write can time out *client-side after the row already committed* (gap H13.1). On any write timeout:
1. **Read back** with `praxis_list_graph` / `praxis_get_context` to check whether the fact landed.
2. If it **did** land → done; do **not** retry (blind retry creates duplicates).
3. If it **did not** → re-add it (singly).

## 3. Decisions & episodes (the *why*, not just the *what*)

When the factory makes a non-obvious choice (chose library X; defaulted Y because the PRD was
silent), record it with **`praxis_record_episode`** — the dedicated decision-log tool. This is also
the home of the "panel-ran" review/audit assertion (§1).

```
praxis_record_episode(
  text="Chose reset-to-0 for the team streak because the PRD was silent on miss semantics.",
  alternatives=["carry-over", "no reset"],
  outcome="pending",                 # later flipped via praxis_record_outcome
  derived_from=[<basis fact ids>],   # the facts/requirements the decision rested on (H5)
  decided_at=<ISO ts, optional>)
```

Episodes are **store-only**: stored whole, append-only, **bypass** dedup/merge/contradiction, and
are **excluded from `praxis_get_context` by default** (H2) so rationale never pollutes semantic
recall. Read them back with `include_episodic=True` on `get_context`, or the episode-log query.
Use `record_episode` — **not** `add_insight(category="episodic")` — for decision journals, and
**not** a local file.

## 4. Tabular ingestion integrity (the H6 audit) — REQUIRED on any table/bulk write

Loss happens at two points: distillation under-emits rows (A), and the deduper over-merges
siblings (B). A is shimmed locally; B is server-side and can only be *caught*, not prevented.

1. **Linearize** tabular input with `agent_factory.tabular.linearize` → atomic,
   lexically-distinct fact sentences (one per row/cell, row+column identity folded in).
   Route the result's `residual_prose` to `praxis_ingest` and each `fact` to `add_insight`.
2. **Write** each linearized fact via `praxis_add_insight`.
3. **Audit the rejected pile.** After the batch, run `praxis_list_graph(state="rejected")`.
   For every row that is a genuinely distinct requirement but was dropped/merged, re-add it
   with more distinctive phrasing (add more of the row's columns into the sentence).
4. **Do not trust** a tabular write until `active + legitimately-merged + rejected` accounts
   for every submitted row.

> Known live example: a standard-deduction table left the "Married filing jointly" row in
> `rejected` while its siblings stayed `active`. Always audit.

## 5. Retrieve for grounding

- Use `praxis_get_context(query, top_k)` for task grounding. Mount the relevant project
  snapshot first (`mount(<project>, prd-<project>)`) so general + project facts compose in one
  ranked result.
- **Cite provenance.** Every decision the agent makes should name the Praxis fact(s) that
  grounded it (the hit's `source`/`score`/`id`). Episodes are excluded by default; pass
  `include_episodic=True` to recall past decisions.
- **`meta` isn't on `get_context` hits** (lean recall path) — read a fact's `meta` + audit trail
  with **`praxis_get_fact(cid)`**.
- Retrieval returns only currently-valid `active` facts. **Pin `as_of` at run kickoff** for
  reproducible runs (point-in-time recall — the whole run sees one stable knowledge version).

## 6. Write-back policy (compounding)

- **Only write a learning that an external signal confirmed** — a passing test/build for
  coding, or human approval for non-coding. Never write speculative "this probably works"
  facts; that poisons the pool.
- Promote a project-pool learning into the **general pool** only when it generalizes beyond
  the one project.
- Write with `on_conflict="surface"` so a contradiction is **surfaced** rather than silently
  overwriting — inspect with `praxis_get_contradictions` and settle with
  `praxis_resolve_contradiction(pair_id, keep=…)`. Three resolutions:
  - `keep="<winner_id>"` — genuine conflict, keep one side (the loser is superseded).
  - `keep="all"` — **false positive** (both genuinely hold, e.g. different actors/scopes): keeps
    **both `active`**, clears the pending edge. This is the non-lossy dismiss — use it instead of
    `custom_text` for false positives.
  - `custom_text="…"` — replace the cluster with one reconciled fact.
- **Close the compounding loop with outcomes (H1).** After a learning's suggested action is
  verified, call **`praxis_record_outcome(fact_id, "succeeded"|"failed")`** — repeated failures sink
  a fact in retrieval, proven facts hold. This is what makes the pool get *more accurate*, not just
  bigger. When a basis fact is invalidated, check **`praxis_get_stale_derivations`** /
  `praxis_dependents` for the learnings now suspect, and re-verify or reject them.

## 7. Never

- Never write code or ephemeral session state into Praxis.
- Never hand-author `build_state`, claims, pins, or check passes — only the plugin
  `_praxis` / `_ticket_state` API writes ticket/check state.
- Never write build/validation/review/audit state to any local file or `.factory/*.json`
  manifest. JSON is static config only.
- Never let a gate fail open: if Praxis is unreachable, BLOCK.
- Never operate in the `praxis` org.
- Never raw-`ingest` tabular input.
- Never write an unverified learning back to the pool.
