---
name: af-build
description: >
  The build entry point: drive this project's incomplete set — the whole prd-<project> build set, or a
  scoped subset — to done. Run the factory build loop (FIND the next incomplete ticket → CLAIM its lease →
  RESOLVE + pin its checks by query → BUILD to the acceptance condition → VERIFY by running EVERY pinned
  validation check on external signals → FINISH only when all checks pass) until no claimable incomplete
  ticket remains, then convene the ce-* cold-eyes WORK-review panel. Verification is intrinsic and
  always-on (the former af-verify), not a separate step. BY DEFAULT it launches an ultracode Workflow that
  fans the dependency-ready frontier out across parallel one-ticket workers (each in its own worktree,
  spawned with the per-ticket worker contract verbatim), looping until the set is done — falling back to a
  single inline agent only for a linear/one-ticket frontier or when Workflow is unavailable; either way
  exactly ONE decision-making agent per ticket, whose only delegation is a disposable read-only retrieval
  sub-agent. All dynamic state lives in Praxis — no JSON status files or locks. The "go work unfinished"
  entry point (not for planning new work).
---

## The methodology — read first, this is the loop af-build OWNS

State lives in ONE place: **Praxis**. There are no JSON status files, no locks on disk, no self-set "done"
flags. A ticket (requirement) and a check are Praxis facts; everything about what is built / claimed /
passed is state **ON THE TICKET'S Praxis node**, read and written live via `hooks/_ticket_state.py` (on
`hooks/_praxis.py`), per `docs/factory-state-contract.md` (METHODOLOGY.md). Conform to that contract
exactly.

**ONE TICKET AT A TIME, END-TO-END.** This is the cardinal rule of the loop. You pop a SINGLE ticket, then
carry it all the way to `finished` — claim → resolve requirements → synthesize covering validations → build
→ validate → release finished — before you so much as read another ticket. No batching, no surveying the
queue, no pre-loading the next ticket's requirements, no holding two tickets in context. The whole-set run
marker + the gate are the *system's* guarantee that the entire scope gets done; your *attention* stays on
exactly one ticket until it has shipped end-to-end. (The one-time scope stamp in step 0 is id-only
bookkeeping — it reads no ticket bodies and is not "working" them.)

**One-ticket-at-a-time is a per-WORKER rule, not a serialization of the run.** By default af-build **fans
the dependency-ready frontier out across parallel one-ticket workers via an ultracode Workflow** (see
*Execution model*, below) — each worker still carries its single ticket end-to-end in an isolated worktree,
and the orchestrator only *schedules* (it computes the ready frontier and dispatches; it never writes code).
That is deterministic scheduling, **not a crew** — a crew is many agents deciding on ONE ticket, which never
happens. The inline single-agent loop (§1→§7) is the fallback for a linear/one-ticket frontier or when the
Workflow tool is unavailable.

To drive the (optionally scoped) build set to done you run **exactly this loop**:

0. **OPEN THE RUN** — resolve the scope to its in-scope incomplete ticket ids (an **id-only** pass — do
   not read ticket bodies) and **STAMP the whole-set run marker** on every one
   (`_ticket_state.stamp_run(cids, owner, scope_label)`). This persisted, scope-bearing marker is what arms
   the gate for the *whole* run — so it keeps blocking even in the instant between finishing one ticket and
   claiming the next. Without it the gate only holds you to a ticket you currently have claimed.
1. **FIND (one)** — query Praxis for the incomplete set in scope (incomplete = never-built | regressed |
   stale, derived from recorded outcomes — including any a validation just regressed), then **pop the ONE
   next DEPENDENCY-READY ticket** with `next_ready_ticket(incomplete)`: the single front whose every
   `depends_on` prerequisite is already `finished` (it depends on no unfinished or in-progress job). Claim
   that one and ignore the rest — you do not look at another ticket until this one ships. Pass the **BARE**
   project name (e.g. `team-app`); the endpoint adds the `prd-` prefix itself — passing `prd-team-app`
   searches `prd-prd-team-app`, returns EMPTY, and silently hides all work.
2. **CLAIM** — atomically flip the ticket's `meta.build_state` `incomplete → in_progress`, stamping
   `claim_owner` = you + a heartbeat. The claim is a **LEASE, not a lock**: refresh the heartbeat while
   working; a stale lease (`now - claim_heartbeat_at > claim_lease_ttl`) auto-reclaims so a dead agent
   never strands a ticket. Parallel agents never double-work because a live claim is visible to all; a
   rare double-claim is harmless wasted work, not corruption.
3. **RESOLVE the validation REQUIREMENTS** — determine which abstract validation *requirements* this
   ticket must satisfy **BY QUERY** (its tag ∪ its surfaces ∪ semantic match against active
   `category="check"` facts). The ticket carries identity only and **NEVER an authored requirement
   list**. Truncate any prior validations and **PIN the resolved requirement ids as the coverage
   contract** (`start_ticket` does claim + resolve + pin-the-contract in one call).
4. **SYNTHESIZE the VALIDATIONS** — convert the retrieved requirements into a **custom list of concrete,
   executable validations that FAITHFULLY COVER every requirement** (each validation declares the
   requirement id(s) it `covers` and a `run` command whose exit code is the signal), then
   `pin_validations(cid, [...])`. A coverage-back-check (`coverage_gap(cid)` must be empty) is part of
   doneness: a requirement with no covering validation means the ticket is **not** verifiable-done.
5. **BUILD** — do the work to satisfy the ticket's binary acceptance condition.
6. **VERIFY** — run **EVERY** pinned validation; record each pass **ON THE TICKET NODE** (never on the
   requirement fact — requirements are read-only during builds). **External signals only** (exit codes /
   tests / build / type-check / lint); never self-judge. This is intrinsic — the build ALWAYS verifies.
7. **FINISH** — only when coverage is complete **and** every pinned validation passed: record a
   `succeeded` outcome and release the lease with the hard enum `build_state="finished"` (which also
   clears the run marker on that ticket). If any validation fails, record a `failed` outcome — that
   **regresses** the ticket so it re-enters the FIND set and is re-done. A requirement that genuinely
   **cannot** be covered or run (credential-only, unsatisfiable) → `block(cid, owner, reason)`: surfaced
   for owner action, excluded from churn, never a silent forever-deadlock.
8. **LOOP** — repeat FIND→FINISH until the scoped incomplete set is empty, `refresh_run` at each ticket
   boundary so the marker never goes stale mid-run.
9. **REVIEW + CLOSE THE RUN** — at done, convene the ce-* cold-eyes **WORK-review** panel over the whole
   diff, record the panel-ran episode, then `clear_run(cids, owner)` to end the run and let the gate go
   inert.

**Praxis is a HARD dependency.** If `_praxis` raises `PraxisUnreachable`, STOP — never assume a ticket is
done, never proceed past a gate, never invent or cache state. The single Stop hook
**`hooks/build_completeness_gate.py`** enforces this loop: it reads Praxis live, **fails CLOSED**, arms when
**this session owns a live `in_progress` claim OR a non-stale whole-set run marker** scopes work to it,
honors `build_state="finished"` (and excludes/ surfaces `build_state="blocked"`), and blocks the turn from
ending until the **entire scoped set** is finished — not merely the ticket you currently hold. The run
marker is what closes the between-ticket window; that is why step 0 stamps it and step 9 clears it.

**There are NO `.factory/*.json` manifests.** "A build run is active" ≡ *this session owns a live,
unfinished `in_progress` claim*, read from Praxis — never a file flag. Code lives in **git**, not Praxis;
only judgments and learnings go to the graph. Every step is an event-log entry — cite the fact(s) that
grounded each decision.

---

# Factory Build — drive the (optionally scoped) build set to done

The explicit entry point for *"address unfinished work."* This skill consumes a plan already hardened by
**af-plan** (→ `prd-<project>`) and surfaces bound by **af-intake-plan**; it does **not** plan new work or admit
requirements. It runs the loop above per ticket and convenes the holistic panel at completeness.

## Scope (optional — the whole point of the argument)

- **No argument** (`/af-build`) → drive the **WHOLE incomplete set** to done. Default.
- **A scope argument** (`/af-build auth` · "only the unfinished auth tickets") → claim and build **ONLY**
  the incomplete tickets matching that scope; leave every other ticket alone **even if it is also
  incomplete**. Resolve the scope to a requirement set, in this order: a **class tag** (match `meta.tags`),
  explicit **requirement ids**, or a named **area** (semantic/text match — e.g. "auth" → login, signup,
  logout, JWT/session, password reset, authz). **List exactly which tickets you selected** before
  building, and **report the non-scoped incomplete tickets as parked** — surfaced, never silently skipped,
  but not claimed this run. If the scope is ambiguous, list your selection and ask before churning.

**The resolved scope IS the run, and the gate enforces exactly it.** Whatever set you select — all, a tag,
specific ids, or an area — `stamp_run` marks precisely those ticket ids (step 0). The whole-set gate then
blocks until **every marked ticket** is `finished` (or `blocked`), and the parked non-scoped tickets carry
no marker so the gate leaves them alone. Scope is therefore a hard contract, not advisory: you cannot end
the run with a marked ticket unfinished, and you cannot accidentally over-build a parked one.

## Validation source — the project space's `building-validation` snapshot

Validation **checks live in a DEDICATED snapshot inside the project's own space**, separate from the
`prd-<project>` snapshot that holds the tickets and their build state. **A project IS a space** — the
space id is the BARE project name (`team-app`), and inside it live the `prd-<project>` snapshot
(tickets, mutable) and the check snapshots.

**Two snapshots, one space.** At RESOLVE time af-build reads the *checks* from
`(space=<project>, snapshot=building-validation)`, while every bit of ticket STATE — claims, pins,
passes, outcomes, run-markers — is read and written on the **`prd-<project>`** snapshot. The typed
`project_ref` seam in `hooks/_ticket_state.py` (`resolve_validation_requirements` / `start_ticket`)
points ONLY the check reads at `building-validation`; check resolution never touches the state snapshot.
That check snapshot must hold the `category="check"`, `scope="validation"` rules; if it is empty a ticket
resolves **only** its always-present acceptance-condition floor (below) — fewer checks, never a crash.
(Seed it from the plan or save a snapshot into it out-of-band; af-intake-build-validation is how new
`building-validation` rules get authored there.)

### How a check pins onto a ticket — the matching model

Every check **owns its own applicability**. The applicability **PREDICATE** is the check's
`meta.applies_to` — a list of tags. The **IDENTITY** it matches against is the ticket's `meta.tags`
(with the ticket's `meta.applies_to` as a lenient fallback for a ticket that carries no `tags`). **A
check pins onto a ticket iff their tag sets intersect** — one shared tag is enough.

Both sides are **normalized on both ends** — at author time (when the check or ticket is written) AND at
resolve time (when af-build runs the query) — by the same rule: `strip` + `casefold`, with the literal
`"*"` preserved verbatim. So `Auth`, `auth`, and ` auth ` are the same tag, and a check is **never
silently dropped** over casing or stray whitespace.

### The lanes that build the contract

RESOLVE unions three **precise, mandatory** lanes, then prepends the floor:

- **tag lane** — checks whose `meta.applies_to` intersects the ticket's (normalized) tags. This is the
  intersection rule above.
- **`"*"` wildcard lane — SEPARATE on purpose.** Universal gates authored with `applies_to: ["*"]`
  (typecheck, build, lint, test) that apply to EVERY ticket. This lane is queried **separately** because
  the per-tag lookup in the tag lane *structurally cannot* surface a `["*"]` check: a ticket's concrete
  tags are things like `auth`, `backend` — they never include the literal `"*"`, so intersecting a
  ticket's tags with `["*"]` is always empty. Pulling wildcards explicitly is the only way a universal
  gate reaches every ticket.
- **surface lane** — checks bound via the `renders` edge to a surface the ticket renders, so a
  frontend/UI check lands ONLY on tickets that render a screen and never on a pure backend ticket.

The **semantic lane is separate and advisory** — retrieved as *inspiration* during synthesis, never
pinned, never gating completion (§3).

### The acceptance floor is always prepended

`contract_with_floor` ALWAYS puts the ticket's own binary acceptance condition (`<cid>::acceptance`) at
the front of the contract, so **the contract is never empty even when zero Praxis checks match**. Every
ticket therefore has at least one thing to prove: its own red→green acceptance test.

### Two worked examples

- **A check-matched ticket.** A backend ticket tagged `[backend, token-verification]` resolves a
  `backend`-tagged typecheck (tag lane: `backend` is in both sides) and a `token-verification` login-e2e
  check (tag lane: `token-verification` is in both) — **PLUS** the always-prepended `<cid>::acceptance`
  floor. Contract = 3 requirements; every one must be covered and pass before FINISH.
- **A zero-declared-check ticket.** A ticket whose tags match nothing in `building-validation` (and which
  renders no bound surface) resolves the floor ALONE — just `<cid>::acceptance`. **This is NOT a defect
  and needs no amend.** You still author the custom red→green eval for the acceptance condition and finish
  normally; a floor-only contract is a complete, honest contract.

### Verify coverage BEFORE a build — the dry-run inspector

`python -m agent_factory.tools.resolve_preview <project>` prints, **read-only**, exactly which checks pin
onto which tickets and by which lane, without claiming or building anything. It is the **formal way to
verify coverage** — run it before a build whenever you want to see the resolution the loop will compute.

> **Renamed from `coding-validation`.** The build-check snapshot is now `building-validation`, and it is
> a per-project snapshot in the project space — NOT a single global `coding-validation` space. Legacy
> global checks are not retro-fitted into per-project spaces (old data carried no reliable project
> association); teams re-seed each project's `building-validation` snapshot via af-intake-build-validation.

**Override — slash argument ONLY** (no env seam): `/af-build [scope] --checks-space=<space[:snapshot]>`
points resolution at a different `(space, snapshot)` for this run. Thread it as an `override`
`(space, snapshot)` pair into **every** `start_ticket(...)` call — including the per-ticket worker
contract (§8), so fanned-out workers read the same reference. With no argument the default applies:
`space=<project>`, `snapshot=building-validation`.

## ORG TENANCY — operate in the PROJECT-DERIVED org, never a hardcoded default

Every Praxis read/write this loop makes is tenanted to an **org**, and that org is **project-derived** — the
`PRAXIS_ORG` pin, resolved through `identity.factory_org()`, NOT a hardcoded `"agent-factory"`. A fresh run
proceeds in the project's pinned org and **never selects `"agent-factory"`** (or any literal) just to "get
going". **Hard rule: the af-build hook-client org (`PRAXIS_ORG`) and the MCP-tool org (`praxis_whoami` /
`praxis_select_org`) MUST AGREE** — a fail-loud guard enforces it, so a header-truthful `whoami` that
disagrees with the pinned client org is a STOP, not something to paper over by re-selecting an org. If they
diverge, align them to the one true project org (fix the pin, or fix the selection) **before** claiming a
single ticket; `praxis_select_org` itself refuses a request that fights the `PRAXIS_ORG` pin, naming both
orgs. Never select around the mismatch.

## STATE TENANCY — the whole loop operates on the plan snapshot

Ticket STATE (build_state, claims, pins, run-markers, outcomes) lives on the project's
`prd-<project>` snapshot, NOT working memory. Compute the plan ref ONCE at the top of the run:
`PLAN = _ticket_state.project_ref(project).plan` (== `(project, "prd-<project>")`). Then:
- pass `space=PLAN[0], snapshot=PLAN[1]` to `_praxis.incomplete_requirements(project, ...)` (FIND) and to
  `_praxis.record_outcome(...)`;
- pass `ref=PLAN` to every `_ticket_state` state call — `stamp_run`/`refresh_run`/`clear_run` here, and
  `claim`/`heartbeat`/`release`/`block`/`pin_*`/`record_validation_pass` in the worker (§8).
`start_ticket(cid, owner, project)` derives PLAN from `project` itself. Working memory is only the
dashboard's edit buffer + personal-memory MCP surface; the factory never keeps state there.

## 0. OPEN THE RUN — stamp the whole-set marker

Resolve the scope (§Scope) to its in-scope incomplete ticket ids, **list them for the human**, then
`_ticket_state.stamp_run(cids, owner, scope_label, ref=PLAN)`. This is the single act that makes the gate
enforce the *whole* run rather than just a held claim. `refresh_run(cids, owner, ref=PLAN)` at every ticket
boundary keeps the marker non-stale (it auto-expires after `DEFAULT_RUN_TTL_S` so a dead run never strands
the set), and `clear_run(cids, owner, ref=PLAN)` at the very end (§7) ends the run.

## Execution model — /af-build LAUNCHES the ultracode Workflow (NOT optional, NOT your discretion)

**Invoking `/af-build` IS your explicit authorization AND instruction to call the `Workflow` tool.** The
Workflow tool's "only run when the user opted into multi-agent orchestration" rule is ALREADY SATISFIED the
moment this skill runs — *"the user invoked a skill or slash command whose instructions tell you to call
Workflow"* is exactly one of the sanctioned opt-ins, and this is that skill. So do NOT hesitate, do NOT ask
for permission, and do NOT quietly grind tickets one-by-one just because the user didn't type "ultracode":
**launching the fan-out Workflow is the default, mandatory behavior of this command.** Grinding the set
inline when the ready frontier is 2+ wide is a BUG, not a safe choice.

After §0 stamps the run marker, compute the dependency-ready frontier (id-only, no bodies):
`_praxis.incomplete_requirements(project, space=PLAN[0], snapshot=PLAN[1])` → filter to the marked ids →
`_ticket_state.ready_tickets(...)`. Then, unconditionally:
- **≥2 tickets ready → LAUNCH THE WORKFLOW (the script below). ALWAYS — this is the whole point of the
  command.** The lease + the `depends_on` DAG make parallel isolated workers safe, and it is dramatically
  faster than serial. If you choose NOT to fan out, you MUST name which of the two narrow exceptions below
  applies, in your reply — silence is not an option.
- **≤1 ready** (a strictly-linear DAG, or a single remaining ticket), **OR the `Workflow` tool is genuinely
  absent from this session's tools** → and ONLY then → run the inline per-ticket loop (§1→§7) yourself. A
  fleet buys nothing on a one-wide frontier. These two are the ONLY sanctioned inline paths.

**§1–§7 below ARE the per-ticket worker contract** — the exact loop each parallel Workflow worker runs (the
§8 block hands it to them verbatim, one worker per ready ticket). Read them as *what the workers do*, not as
*what you do sequentially*. You run §1–§7 inline ONLY under the narrow exception above. Either way YOU own
the run marker and the gate: `build_completeness_gate` armed on YOUR session in §0 and BLOCKS your turn from
ending until the whole marked set is `finished` — that is the hook that forcibly keeps the build rolling.

**What the workflow does** (deterministic scheduling — each ticket still has exactly ONE decision-making
worker; never a crew):
1. Compute the current dependency-ready frontier (a cheap read-only dispatcher agent runs the hooks).
2. Fan out **one worker per ready ticket**, each in its **own git worktree** (`isolation:'worktree'`) so
   parallel file edits never clobber, each spawned with the **§8 per-ticket worker contract VERBATIM** —
   that block is what makes each worker EVAL-FIRST (red→green) and lease-safe. Do not paraphrase it.
3. As workers finish, their tickets flip to `finished` in Praxis, which **unlocks dependents** — loop back
   to (1) and dispatch the newly-ready frontier (loop-until-dry). Repeat until the frontier is empty.
4. A round with **`ready:[]` while work remains** is a **dependency stall** (a cycle, or a chain rooted on a
   `blocked` ticket) — break it exactly as §1 says (unblock the root, fix a bad `depends_on`, or `block()`
   the unsatisfiable dependents). Do not spin.

**You own the run marker and the gate.** You stamped it in §0, so `build_completeness_gate` arms on YOUR
session and blocks your turn from ending until the whole marked set is `finished` — even though the workers
build. **Await the workflow** (`run_in_background:false`); its completion is the whole job. `refresh_run`
the marker across a long run.

**Integrate, then review.** Worktree workers leave changes in per-ticket worktrees. After the workflow
returns, **integrate the finished tickets' worktrees onto the run's working tree** (merge each; resolve the
rare conflict when two same-round tickets touched one file — dependency-independent is not file-disjoint),
then run the **WORK-review panel (§7)** over the integrated diff and `clear_run`.

**Canonical build-churn workflow — author it inline (substitute PROJECT / SCOPE / OWNER):**

```javascript
export const meta = {
  name: 'af-build-churn',
  description: 'Drive the scoped incomplete set to done: fan out the dependency-ready frontier as isolated per-ticket workers, loop until dry.',
  phases: [{ title: 'Build' }],
}
const project = args.project            // BARE name (no prd- prefix)
const scope = args.scope || 'ALL'
const owner = args.owner

const FRONTIER = { type: 'object', required: ['ready', 'remaining'], additionalProperties: false,
  properties: { ready: { type: 'array', items: { type: 'string' } }, remaining: { type: 'integer' } } }

// The §8 per-ticket worker contract, VERBATIM, with only PROJECT/TICKET/OWNER/CHECKS_SNAPSHOT substituted.
// Space is always the project; snapshot defaults to building-validation (or the run's --checks-space override).
const WORKER = (cid) => `<<< the full §8 block, TICKET=${cid}, PROJECT=${project}, OWNER=${owner}:${cid}, CHECKS_SNAPSHOT=building-validation >>>`

let guard = 0
while (guard++ < 200) {                  // runaway backstop, far above any real frontier depth
  const f = await agent(
    `Read-only — issue NO claims/edits/writes. For PROJECT="${project}" (BARE), scope="${scope}": run ` +
    `_praxis.incomplete_requirements(project), filter to the scope's marked ids, then ` +
    `_ticket_state.ready_tickets(...) (every depends_on finished; exclude live leases). ` +
    `Return {ready:[cid,...], remaining:<in-scope incomplete not-yet-finished count>}.`,
    { phase: 'Build', label: 'frontier', schema: FRONTIER, effort: 'low' })
  if (!f || !(f.ready || []).length) break            // empty frontier -> done (or a stall to surface)
  await parallel(f.ready.map(cid => () =>
    agent(WORKER(cid), { phase: 'Build', label: `ticket:${cid}`, isolation: 'worktree' })))
  // finished tickets unlock dependents; the next iteration re-queries the frontier
}
return { done: true }
```

## 1. FIND — pop the ONE next dependency-ready ticket

**Work exactly one ticket at a time, end-to-end.** FIND pops a SINGLE ticket; you then carry it all the way
to `finished` (§2→§6) before you look at, read, or claim any other. Do not survey the queue, pre-read other
tickets' requirements, or hold a batch in mind — one ticket is the entire working set until it ships.

Call `_praxis.incomplete_requirements(project, space=PLAN[0], snapshot=PLAN[1])` with the **BARE** project
name (PLAN binds it to the plan snapshot — §State tenancy). The server derives this view from outcomes +
staleness + lease state, so a validation that just regressed a ticket already shows up here — no local sync,
no manifest. To skip tickets another live session already holds, pass `exclude_leased=True`.
Filter to the **marked scope** (the ids you stamped in §0), then **pop the single front** with
`_ticket_state.next_ready_ticket(incomplete)` — the one ticket that is not finished, not blocked, and
depends on **no unfinished or in-progress job**. Claim that one; ignore the rest.

- **Readiness is computed over the WHOLE incomplete set**, not just your scope, so a cross-scope
  prerequisite still gates correctly. (`ready_tickets`/`pending_deps` exist for the gate's report and for
  choosing among equally-ready candidates — not for batching work.)
- **`next_ready_ticket` returns None but work remains** → a **dependency stall** (a cycle, or a chain rooted
  on a `blocked` ticket). Do not spin: fix/unblock the root prerequisite (af-intake-plan amend / accept), correct
  a wrong `depends_on` edge, or `block()` the unsatisfiable dependents. The gate detects + surfaces this too.
- **`next_ready_ticket` returns None and nothing is waiting** (only `finished` + `blocked` remain) → the
  scoped set is done; go to the WORK-review panel (§7).

## 2. CLAIM + RESOLVE REQUIREMENTS — one transaction per ticket

For the next claimable ticket call `_ticket_state.start_ticket(cid, owner, project)` (BARE project name;
pass an `override=(space, snapshot)` pair too when this run overrides the default — the project space's
`building-validation` snapshot — §Validation source).

**Pre-claim resumability guard (falsifiable "Praxis = sole state").** BEFORE it leases, `start_ticket`
resolves the requirement set and runs the pure structural resumability probe
(`agent_factory.resumability.resumability_report`) over the ticket's meta: is a cold worker able to
reconstruct what "done" means from Praxis state ALONE? A ticket is resumable iff it is
**coverable-from-state** (`non-empty acceptance` **OR** `non-empty resolved required_validations` — the
same rule `contract_with_floor` uses, so a check-covered but acceptance-less backend/terminal ticket is
NOT starved) **AND** carries a `verify` mode. If the probe FAILS, `start_ticket` does **not** claim: it
stamps `meta.under_specified = [missing fields]` (a planning defect surfaced to intake, never a silent
skip) and returns `None`. Fix the gap at intake (add an acceptance condition OR a declared check); the
next `start_ticket` then clears the marker and claims. A resumable ticket claims and proceeds unchanged.

On a resumable ticket, `start_ticket` does three things atomically:

1. **Claim the lease.** `incomplete → in_progress`, stamping `meta.claim_owner`, `meta.claim_at`,
   `meta.claim_heartbeat_at`, `meta.claim_lease_ttl` (default `DEFAULT_LEASE_TTL_S = 900`) via the
   race-tolerant `patch_meta` read-modify-write. Returns `None` if a live lease already holds it (or the
   ticket is `blocked`) — skip it.
2. **Resolve the MANDATORY (precise) requirements — a fresh QUERY, never a list authored on the ticket.**
   The ticket carries identity only (its tags/surfaces); the requirement set is computed live from the
   `category="check"` facts in `(space=<project>, snapshot=building-validation)` — read there while ticket
   state stays on `prd-<project>`. `resolve_validation_requirements` returns the de-duplicated union of
   three **precise** lanes (the full matching model is in §Validation source):
   - **tag match** — a check pins iff its `meta.applies_to` (the applicability PREDICATE) intersects the
     ticket's `meta.tags` (the IDENTITY; the ticket's `meta.applies_to` is the lenient fallback). Both
     sides are normalized `strip`+`casefold` with `"*"` preserved, on BOTH the author side and the
     resolve side — so `Auth` vs `auth` never silently drops a check.
   - **`"*"` wildcard** — universal gates (typecheck/build/lint/test) authored `applies_to: ["*"]`, pulled
     as a SEPARATE lane because a per-tag query can never match a `["*"]` check: a ticket's concrete tags
     never include the literal `"*"`.
   - **surface match** — requirements bound via the `renders` edge to a surface the ticket renders, so a
     frontend/UI check lands ONLY on tickets that render a screen and never on a backend ticket.

   These are abstract *"what must be proven"* facts — declarative and read-only during a build — and they
   are **mandatory**: the coverage contract (§3) forces every one to be covered. (The fuzzy **semantic**
   lane is separate and ADVISORY — §3.)
3. **Pin the contract = resolved checks PLUS the acceptance-condition FLOOR.** `start_ticket` calls
   `contract_with_floor` then `pin_requirements`: it always prepends the ticket's **own binary acceptance
   condition** (`<cid>::acceptance`) as a requirement, so the contract is **never empty even when zero
   Praxis checks match**. This is what makes "the validation agent generated no evals" impossible to wedge
   on: there is always at least one thing to validate — the red→green acceptance test. It **TRUNCATES** any
   prior validations and writes `meta.required_validations` with an empty `meta.pinned_checks`; synthesis
   (§3) fills that in. A ticket with no checks AND no acceptance condition is an empty contract — but the
   pre-claim resumability guard (above) now catches that case FIRST, routing it to `under_specified`
   (returns `None`, never claimed) instead of letting it reach an empty pin. So `start_ticket` returns
   `None` for two reasons — a live lease already holds the ticket, **or** it was routed under-specified
   (check `meta.under_specified`); neither is a claim, so skip it and (for the latter) surface it to intake.

## 3. SYNTHESIZE the validations — convert requirements into a custom covering set

**First, pull the ADVISORY candidates (the semantic lane) as inspiration.** Before authoring, call
`retrieve_advisory_checks(cid, project, scope="validation")` (same `(space=<project>, snapshot=building-validation)` seam) — a hybrid
retrieval of `category="check"` facts semantically close to THIS ticket's text. They are **inspiration,
NOT the contract**: fold the genuinely-relevant ones into the validations you author, and **ignore the
rest** — an irrelevant retrieval is harmless precisely because it never gets pinned and never gates
completion. This is the "search the DB for candidate checks, then let the LLM curate" step; the hard
guarantee stays on the mandatory precise set (§2.2), the recall boost comes from here.

**Then consider the SEEDED generic candidates (the deterministic lane).** `agent_factory/seeded_checks.toml`
is a hand-curated library of generic reusable checks (correctness, security, error-paths, maintainability —
each a binary command or a graded rubric) offered to EVERY ticket via `seeded_candidates(ticket_tags)`. Unlike
the semantic lane these are surfaced deterministically (not embedding-dependent), but they are equally
**opt-in and non-gating**: fold in the ones genuinely relevant to this ticket as authored validations, ignore
the rest. A graded seeded candidate becomes a `kind:"graded"` validation carrying its rubric (see §5 VERIFY).
`python -m agent_factory.tools.resolve_preview <project>` lists the seeded candidates offered per ticket.

**Then GATHER + ASSEMBLE the graded candidate pool (the shared pool; the gating function).** Beyond the
semantic lane above, the `building-validation` pool holds `candidate:true` graded checks contributed by
BOTH `af-intake-plan` (whole-plan B1 findings) and your own ticket-local search — two writers, one pool.
The mandatory-vs-advisory decision is made HERE, by a function, not by either writer:

1. **ADD your discoveries to the pool (U4).** If your rules/memory search surfaces a ticket-specific
   quality concern worth grading, PERSIST it as a `candidate:true` graded check via
   **`af-intake-build-validation`** (never a direct write — preserves the single-writer lock), scoped
   TIGHTLY to this ticket's tags/surface (never `["*"]`), `authored_by:"build"`, with a `severity` hint.
   Idempotent on `check_id`, so re-discovery updates in place.
2. **READ the pool for this ticket.** `pool_candidates(cid, project, scope="validation")` (hooks/) — the
   DETERMINISTIC set of every `candidate:true` check resolving onto this ticket (NON-gating; the full
   set, unlike the semantic `retrieve_advisory_checks` sample).
3. **ASSEMBLE the per-ticket rubric — the function that determines what gates (U5).**
   `from rubric_assembly import assemble` (src/), then
   `graded = assemble(pool_candidates(...), budget=<N>, covers=[<this ticket's requirement_id>])`. It
   promotes the highest-`severity` candidates (up to `budget`) to individual GATING graded validations
   and folds the rest into ONE min-of-candidates advisory aggregate. Deterministic — the gating set is
   stable across passes on the same pool (no thrash). Include `graded` in the list you `pin_validations`.

The promoted graded validations gate via `all_validations_passed` like any pinned validation; the
aggregate is soft-floored (advisory unless a folded concern is egregious). Neither `af-intake-plan` nor
you chose which candidates gate — `assemble` did, at build time, from the pooled severity hints. All
graded validations are judged in **§5 VERIFY** (`verify_graded_check`, fresh-context judge), never run.

This is the heart of the two-tier model. The retrieved requirements say *what* must be proven; **you author
the concrete validations that prove it for THIS ticket**, faithfully covering every **mandatory**
requirement (advisory candidates you chose to honor become validations too, but coverage is only enforced
on the mandatory set). For each
requirement decide the executable signal (a specific test command, a type-check, a build, a lint, an AST
parse, a script) and emit a validation entry `{validation_id, covers: [requirement_id, ...], run: "<cmd>"}`.
One validation may cover several requirements and several may cover one — what matters is that the **union of
`covers` equals the full requirement set**. Then `pin_validations(cid, [...])`.

**The contract always includes the `<cid>::acceptance` floor, which is ALWAYS coverable** — it is the
ticket's own binary acceptance condition, so you author the red→green acceptance test for it (write the
failing test, watch it fail, make the change, watch it pass). That single validation alone lets the ticket
finish, so a ticket is never stuck "no evals were generated." Cover the acceptance floor first, then any
additional resolved checks.

`coverage_gap(cid)` must return `[]` before the ticket can finish: a requirement with no covering validation
is an **uncovered contract**, not a pass. If an *additional* requirement genuinely cannot be turned into any
runnable signal (it needs a credential/secret only the owner can supply, or it is unsatisfiable as written),
do **not** fake a covering validation — `block(cid, owner, reason)` the ticket so it is surfaced for owner
action. Never stub or fake a validation to escape coverage. (The acceptance floor itself is unsatisfiable
only if the acceptance condition is — that is a planning defect to `block()`, not to paper over.)

There is **no preflight manifest and no separate env-readiness step.** Environment readiness is just another
requirement you cover with a validation: a missing env var / unauthenticated CLI / unreachable service is a
**failing validation**, and the ticket can't finish until it passes (or is `block`ed if only the owner can
fix it).

**Pin knowledge at kickoff.** Record the run's `as_of` timestamp so every retrieval this run sees one
stable plan even as write-backs land, and **mount read-only** the conventions pool + the project's
`prd-<project>` snapshot. The live graph is this run's scratch; the plan + conventions are mounted, not
copied in.

## 4. BUILD — one decision-making agent

**a. Assemble hermetic context (declare it; don't free-query mid-task).** Up front, pull exactly: the
ticket's requirement + its **binary acceptance condition**, the conventions/invariants it touches, and any
ticket-specific facts — via declared queries (scope + top_k + `as_of`). Budget it (hot constitution always
in; warm/cold to a ceiling well below the context-rot threshold). The agent works from this sealed bundle;
a new need is a new declared pull, logged — never unbounded mid-task querying. For a **screen-scoped
ticket**, pull the governing behavior with `praxis_requirements_for_surface(project, screen_id)` (the
active requirement facts bound to that wireframe screen via `renders`, per af-intake-plan) and take the layout
from the wireframe HTML in git.

**Read-only retrieval sub-agent (the ONE permitted delegation).** When the bundle needs reading many files
or large surfaces, dispatch a *disposable, single-shot* sub-agent to read and return a compact digest — so
the parent window never absorbs raw noise. Hard constraints, or it degrades into a crew:
- **Read-only tools only** (Read/Grep/Glob/LS). It never edits, runs state-changing commands, writes to
  Praxis, or commits.
- **One shot, no dialogue.** It returns once; you never converse with it or chain it into a decision.
- **Cheap model, fixed compact schema.** Output is a curator's digest (*file → role*, the specific
  facts/patterns asked for, constraints/gotchas, what's *still unknown*) — filter ruthlessly, it is a
  curator of insights, not a summarizer.
- You remain the **only** agent that decides, edits, writes to Praxis, or commits. This is context hygiene,
  not orchestration. **Read-fully guard:** any file the human or plan names *explicitly* is read fully in
  your own context first (no limit/offset); only exploratory/bulk reading is delegated.

**b. Re-anchor the goal.** Restate the ticket's acceptance condition at the start of each cycle (and after
any context compaction). Goal drift comes from semantic accumulation, not token count — re-injecting the
objective is the cheap, proven defense.

**c. Act.** The single agent does the work with real tools in the repo (edit, run, search). Make the change
that satisfies the acceptance condition — nothing broader (resist scope creep into adjacent tickets).
`heartbeat(cid, owner)` across long stretches so the lease stays live and isn't reclaimed out from under
you.

## 5. VERIFY — intrinsic, always-on, external signals only

A ticket is **not done because the agent believes it is** — it is done when an external signal says so.
Intrinsic self-correction (the model reviewing its own work) *degrades* coding quality; only signals the
agent cannot fake count. The build **ALWAYS** runs this — it is not optional and not a separate skill.

**Run EVERY pinned validation — exit code is the verdict.** For every entry in `meta.pinned_checks` (your
synthesized validations), run its `run` command and take its **exit code** (0 = pass) / raw output as the
verdict — not the agent's reading of it. Record the result on the ticket:

```
record_validation_pass(cid, validation_id, passed=(exit_code == 0), ran_at=now)
```

This MERGES into the ticket's `pinned_checks` entry via `patch_meta` — **never onto the requirement fact**.

**GRADED validations (`kind:"graded"`) — subjective judgment, still one boolean.** A validation the worker
synthesized from a seeded rubric candidate (see §3 / `agent_factory/seeded_checks.toml`) has no exit-code
command; its verdict is a min-of-axes rubric judgment. Run it through the graded harness instead of a shell
command:

```
from _graded_verify import verify_graded_check   # hooks/
r = verify_graded_check(cid, validation_id, code_diff, complete, ref=PLAN)  # complete = fresh-context judge
if r.should_block:
    block(cid, owner, r.block_reason, ref=PLAN)   # cap / non-convergence → HITL, never incomplete-forever
```

It grades the ticket's diff with a **fresh-context judge** (never the builder's context), records the same
`passed` boolean the gate reads, and **caches the verdict by code-state hash** so identical code is never
re-graded (this is what stops a nondeterministic judge from thrashing the forcibly-continue loop). A graded
check only *fails* on a below-threshold axis or a located, above-confidence-floor defect — vague
dissatisfaction with no located defect passes. The rubric is the copy **frozen** onto the pinned validation
at synthesis time, so editing the seeded library never moves the target mid-ticket. `verify_graded_check`
returns `should_block=True` once the graded iteration cap is hit or the defect set stops shrinking; route
that to `block()` (the existing HITL escalation tier), never an endless retry.

Alongside the pinned validations, run the project's real external gates so the acceptance condition is
actually observable (discover the commands; don't assume):

| Gate | Signal | When |
|---|---|---|
| **Pre-flight** | schema / type-check / lint / AST parse | before trusting an edit |
| **Tests** | the task's acceptance test(s) + the existing suite | the primary oracle |
| **Build** | compile / bundle succeeds | for anything that must build |

- **The acceptance test must exist and must have failed before the change** (red→green). A test written to
  match the implementation proves nothing — if the acceptance condition has no test, write the failing test
  first, watch it fail, then verify the change makes it pass.
- **Nothing about *what* must be proven lives in this skill or any file** — the validation *requirements*
  are resolved by query. This skill says only *how* to synthesize covering validations, run them, and
  record each pass. **The build NEVER waits on af-intake-plan to author per-ticket eval requirements**, and
  af-intake-plan must NOT be asked to author them. The contract a ticket resolves is: its **own acceptance
  condition** (the always-present `<cid>::acceptance` floor — every ticket has one) ∪ any **STANDING
  general validation lenses** already in Praxis (wildcard `applies_to:"*"` / tag-matched conventions, e.g.
  a universal typecheck+build+lint gate). A ticket that resolves *only* the floor is **not** a defect and
  needs **no** amend — you still author a custom eval for its acceptance condition and proceed. (af-intake-plan
  *amend* exists to add a NEW general lens when one is discovered — a compounding improvement — never as a
  prerequisite for building an existing ticket.)

**Whole-repo gates pin on EVERY ticket — leave the repo green with ONLY your slice.** The universal
`applies_to:["*"]` gates (`backend-build`, `backend-vitest`, typecheck, lint, the suite) resolve onto
**every** matching ticket through the wildcard lane, so each isolated per-ticket worker is responsible for
leaving the **whole repo** compiling and its tests green using ONLY its own slice. Make your slice
**self-consistent** — stub or adjust the callers your change touches so the shared build/test stays green
even though a sibling ticket's half has not landed yet — or, if you genuinely cannot go green without a
sibling's change, `block(cid, owner, reason)` and surface it. **NEVER weaken, skip, or scope-down a
whole-repo gate to get your ticket green** — a red shared build is the gate doing its job, not an obstacle to
route around. (A scope-level **integration gate** — running the whole-repo build/test ONCE at end-of-scope
instead of on every ticket — is a possible alternative, but FLAG its tradeoff: intermediate tickets can merge
non-green, so the repo is not guaranteed buildable between tickets. Present it as an option, never the
default.)

**Correction loop — fires ONLY on an external signal.** On a failing gate or pinned validation, re-enter BUILD
(§4c) with the **captured failing signal** as context. Never let "the model decided to revise" be a
transition. Four tiers with explicit trip conditions:
1. **Execute** — one attempt.
2. **Correction** — retry with the failing signal attached. Bounded (a max-attempts cap).
3. **Strategy** — after **N identical failures** (degeneration), stop retrying and replan the ticket.
4. **Human escalation** — after **M replans** without progress, or any low-confidence / irreversible step,
   escalate. Don't loop forever.

A **circuit breaker** trips on repeated identical output or identical errors — that's degeneration, not
progress; escalate rather than burn iterations.

**Structural-erosion check.** Passing tests are necessary, not sufficient: long iterative runs erode
structure (complexity, duplication, file-spread) even while green. Track a per-iteration complexity-delta
(cyclomatic / churn / new-symbol fan-out — wire an existing tool like `radon`/`ruff`/`git diff --stat`,
don't build one) and **halt/escalate** if the delta per unit of verified progress exceeds the task's
budget.

**Separate evaluator / non-coding fallback.** For anything needing judgement rather than a deterministic
signal (rare in coding, common for soft outputs), the evaluator is a **different model from the
generator** — used only for the residue with no deterministic oracle, and only as escalation triage
(proceed vs. park), never as the success verdict for coding. A task type with no deterministic oracle
(form-filling, video) verifies by **human confirmation**: in an unattended run a low-confidence non-coding
step **parks** a checkpoint for batch review; high-confidence steps proceed. For any acceptance criterion
tagged **manual** (af-plan), in an attended run pause and hand it off for human confirmation; in an
unattended run record it as a deferred owned decision and proceed. **A `verify="manual"` requirement's
pass counts only when it carries a human signal** — record it with `record_validation_pass(cid, vid,
passed=True, source="human", ref=PLAN)`, never the default `source="worker"`. `all_validations_passed`
refuses a manual requirement that was only worker-self-certified, so a worker-sourced self-pass leaves the
ticket unfinished by design until a human confirmation lands.

## 6. FINISH — doneness is THE EVAL, recorded as a hard enum (never a count)

The ticket is **finished IFF `all_validations_passed(cid)`** — there is a coverage contract (≥1 required
requirement), **`coverage_gap(cid)` is empty** (every requirement covered), there is ≥1 pinned validation,
and **every** pinned validation `passed == True` (coverage + the synthesized validations ARE the eval).
Then, and only then:

- `_ticket_state.release(cid, owner, state="finished")` — flips `build_state` to the hard enum `finished`,
  NULLs the lease keys, and clears the run marker on this ticket. The single authoritative "done" signal.
- `praxis_record_outcome(cid, success=True)` — recorded too, but it is a **trust/utility signal only** (it
  weights retrieval); it is **NEVER** the completion criterion. A bare success count must never be read as
  "done".

If any pinned validation failed/is unrun, or a requirement is uncovered, the ticket does **not** pass:
- `praxis_record_outcome(cid, success=False)` — **regresses** the ticket so it re-enters the FIND set (the
  fail → regress → re-pick loop; the compounding mechanism).
- `release(cid, owner, state="incomplete")` — yields the lease cleanly so the build loop re-picks it. The
  run marker is **kept**, so the whole-set gate keeps the ticket in scope and forces it to be re-done — a
  clean yield does **not** end the run.

**Yield cleanly** (handing back): `release(cid, owner, state="incomplete")` and say why. **A blocker only
the owner can pass** (a credential/secret, an unsatisfiable requirement): `block(cid, owner, reason)` — this
sets `build_state="blocked"`, surfaces it for owner action, and removes it from the churn set so the run can
complete around it rather than wedging forever. **Never fake a validation pass to escape the loop** —
completeness is outcome-grounded, so the only honest finish is to actually build and pass every covering
validation. Only an externally-confirmed pass is eligible to **write a learning back**: stamp `source` and
`category="learning"`; never write speculative facts and **never block the loop on a write** — queue it and
proceed.

**Infra-dependent verification → `block`, NEVER fake.** A requirement that can only truly verify against
**live infrastructure** — Cognito-token verification against a real pool, the e2e login, a backfill against a
real DB, a federated relink — whose check or acceptance CANNOT honestly go green locally must be
`block(cid, owner, reason)`, surfaced for owner action. It is **never** a stubbed, weakened, or faked-green
validation. Blocking one infra-gated ticket never wedges the run: the single `build_completeness` gate
**completes AROUND blocked tickets** (they are excluded from the churn set and surfaced, not counted as
finished), so the rest of the scope finishes while the infra-gated ticket waits for the owner.

## 7. LOOP, then convene the WORK-review panel

This is the **inline sequential loop** — the fallback when the ready frontier is linear/one-ticket or the
Workflow tool is unavailable (default is the ultracode Workflow fan-out — see *Execution model*). Only
**after the current ticket has shipped end-to-end** (`finished`) do you look at the next: re-query
`incomplete_requirements(project)` (filtered to the marked scope), `refresh_run` the marker, and `FIND` the
**one** next ready ticket (§1), repeating §1→§6 until `next_ready_ticket` returns None and nothing is
waiting (only `finished` + `blocked` left). One ticket fully done, then the next — never two in flight in
*this* agent's context. **By default, though, independent ready tickets ARE fanned out in parallel** — the
lease + DAG make that safe, and the *Execution model* section makes it the default, not a "MAY". **A
fanned-out worker is a GENERIC sub-agent that does NOT read this skill** — it follows only the prompt it is
handed, so the EVAL-FIRST / red→green ordering survives fan-out only if it travels IN that prompt. Therefore:
**spawn EVERY parallel worker (whether via the workflow script or by hand) with the canonical
[per-ticket worker contract](#8-the-per-ticket-worker-contract-spawn-every-fanned-out-worker-with-this) (§8)
verbatim**, one ticket per worker, each in its own worktree. Do NOT paraphrase the loop into a bespoke worker
prompt — copy the contract block. (A worker that builds first and tests after is the exact drift this closes;
it has happened.)
*"Are we done?"* is **not** a counter you maintain: the one `build_completeness_gate` answers it live against
Praxis, blocking until the whole marked set is finished.
After the panel (below), `clear_run(cids, owner)` to end the run; any ticket left `blocked` is surfaced to
the human as needing owner action, never silently dropped.

When the scoped set is empty, convene the holistic **cold-eyes WORK-review panel** over the whole
artifact — the emergent, cross-cutting defects (a source/scope contract inconsistency, an unsatisfiable
target) that per-item checks structurally can't see. **A model judging its own output inflates its own pass
rate**, so the panel is **independent sub-agents** spawned via the Agent tool — never the agent that wrote
the code grading itself.

**compound-engineering is a HARD required dependency** and its ce-* reviewers ARE the default panel — not a
"use if installed" preference. **PRESENCE CHECK first:** verify the ce reviewer agents resolve via the
Agent tool / `/code-review`. If **absent** (compound-engineering not installed/enabled), **do NOT proceed
and do NOT record a panel-ran episode** — surface the remediation
(`claude plugin install compound-engineering@compound-engineering-plugin` / `/reload-plugins`); a missing
panel is a **blocked review**, never a silent pass.

**Surface:** the full diff for the build (`git diff` against the build's base) + the touched modules in
context. **Lenses (≥1 independent reviewer each):**

| Lens | ce subagent type |
|---|---|
| architecture / strategy | `ce-architecture-strategist` |
| correctness | `ce-correctness-reviewer` |
| security | `ce-security-reviewer` |
| maintainability | `ce-maintainability-reviewer` |
| performance | `ce-performance-oracle` |
| testing | `ce-testing-reviewer` |

Don't reinvent these — `/code-review` already merges/dedups their tiered output; either drive it or spawn
the subagents directly. **Dedupe** (merge multiple angles into one finding per distinct defect, carry the
strongest severity) BEFORE emitting. **Emit each finding as an `incomplete` Praxis ticket/check** bound to
the touched area: a defect demanding a fix → a **ticket** (the build loop re-opens via FIND and the
completeness gate stays blocked until it is `finished`); a recurring "this must be proven" rule → a
**check** (af-intake-build-validation, which also regresses the matching finished tickets). That is the
entire enforcement mechanism — no second gate, no advisory-only suggestions. **Closing a finding** = its
ticket/check reaching `build_state="finished"`: **resolved** (built + checks pass) or **accepted** (a
conscious owned trade-off, recorded as a Praxis episode before the ticket is released `finished` — never
silently dropped).

**Panel-ran assertion — the only residue.** After the panel runs, record exactly **one**
`praxis_record_episode` (phase `work`, the project, the panel composition, the count of findings emitted) —
an assertion that reviewing happened, so it can never be silently skipped.

**SKIPPABLE — explicit policy, never silent.** Compute a size/risk signal: `small` = changed lines under
threshold (~400) **AND no high-risk area touched** (auth/authz, payments, secrets/config,
migrations/data-lifecycle, deploy/CI — any of these forces non-small). **small + attended** → propose skip;
human confirms → record a skip episode. **small + unattended** → auto-skip → record a skip episode
(`"auto-skip: small/low-risk, unattended"`). **NOT small** → review is mandatory (a human MAY force-skip
only with an explicit recorded reason). A skip is the *absence* of a panel-ran episode plus the *presence*
of a skip episode; never fabricate a panel-ran assertion and never edit config to get past the panel.

## 8. The per-ticket worker contract (spawn every fanned-out worker with THIS)

This is the **single canonical, verbatim** statement of the per-ticket loop — the same EVAL-FIRST ordering
§1–§6 walk, condensed into one self-contained prompt that **travels with a spawned worker**. The §1–§6 prose
is for *you* (the orchestrator, who read this skill); this block is for the **generic sub-agent** you fan a
ticket out to, which has not. When fanning out (§7), **spawn each worker with the block below copied
verbatim**, substituting only `PROJECT` / `TICKET` / `OWNER`. Do not paraphrase it.

The lifecycle calls below are **code-enforced** in `hooks/_ticket_state.py` (per
`docs/factory-state-contract.md`): `start_ticket` truncates prior evals + pins the resolved requirement
contract (incl. the acceptance-condition floor), and `release(state="finished")` is **refused** unless
`all_validations_passed`. The worker **calls** them — it never reinvents or works around them, and never
fakes a pass.

```text
You are an af-build per-ticket worker. Build EXACTLY ONE ticket, EVAL-FIRST (red→green). You own only
this ticket — never look at, claim, or build another. Inputs: PROJECT=<bare name>, TICKET=<cid>,
OWNER=<your session id>, CHECKS_SNAPSHOT=<building-validation | the run's --checks-space override
snapshot>. Checks resolve from (space=PROJECT, snapshot=CHECKS_SNAPSHOT). Run helpers from
hooks/_ticket_state.py (contract: docs/factory-state-contract.md). af-intake-plan is NOT in this path — it
does not author eval requirements at build time; never wait on it.

TICKET STATE lives ON THE PLAN SNAPSHOT, never working memory: let PLAN=(PROJECT, "prd-"+PROJECT) and
pass ref=PLAN to EVERY _ticket_state state call below (pin/coverage/heartbeat/record/all_/release/block).
start_ticket takes PROJECT and derives PLAN itself, so it needs no ref. Missing the ref on any one call
splits that write into working memory and the ticket never reads back as done — always pass ref=PLAN.

1. CLAIM + RESOLVE + TRUNCATE  — start_ticket(TICKET, OWNER, PROJECT, override=(PROJECT, CHECKS_SNAPSHOT)).
   This atomically claims the lease (on PLAN), resolves the eval REQUIREMENTS (tag ∪ surface from the project
   space's CHECKS_SNAPSHOT ∪ the ticket's own acceptance-condition floor), TRUNCATES any prior evals, and pins
   the fresh requirement contract. If it returns None → the ticket is taken/blocked,
   stop. If it returns an EMPTY list (no checks AND no acceptance condition) → block(TICKET, OWNER, reason, ref=PLAN)
   and stop; there is nothing to prove.
2. READ THE CODE  — read the ticket's acceptance condition and the specific files/surfaces it touches, so
   your evals fit THIS code case (real paths, real commands) — not generic placeholders. Do not edit yet.
3. AUTHOR + PIN EVALS  — first pull INSPIRATION: retrieve_advisory_checks(TICKET, PROJECT, scope="validation")
   — semantically-related candidate checks; fold in the relevant ones, ignore the rest (they never gate).
   Then write CUSTOM executable validations that COVER every MANDATORY resolved requirement (each declares
   covers:[req_id] and a `run` command whose exit code is the verdict). pin_validations(TICKET, [...], ref=PLAN).
   coverage_gap(TICKET, ref=PLAN) MUST be empty before you continue (coverage is enforced on the mandatory set only).
4. CONFIRM RED  — run every pinned eval NOW, BEFORE writing any implementation. The acceptance test MUST
   FAIL (red) for the right reason. An eval that passes before you write code proves nothing — fix it until
   it genuinely fails. Do NOT write implementation in this step.
5. BUILD  — only now make the change that satisfies the acceptance condition; nothing broader.
   heartbeat(TICKET, OWNER, ref=PLAN) across long stretches so the lease stays live.
6. CONFIRM GREEN + RECORD  — re-run every pinned eval and the project's real external gates (typecheck /
   build / lint / suite). record_validation_pass(TICKET, vid, passed=(exit_code==0), ran_at=now, ref=PLAN) per eval.
   On a failure, RE-ENTER step 5 with the captured failing signal as context — never revise from self-doubt
   alone, never weaken an eval to pass.
7. FINISH  — when all_validations_passed(TICKET, ref=PLAN) is True: release(TICKET, OWNER, state="finished", ref=PLAN).
   The release is REFUSED while any eval is unrun or red — that refusal is the contract, not an error to route
   around. To yield without finishing: release(TICKET, OWNER, state="incomplete", ref=PLAN). Credential-only /
   unsatisfiable: block(TICKET, OWNER, reason, ref=PLAN).

NEVER build-first / test-after. NEVER fake, delete, or weaken an eval to get green. NEVER finish without
all_validations_passed. NEVER ask af-intake-plan to author the eval requirements.
```

## Long-horizon control (so the run survives length)

- **Disposable agent:** keep durable state in Praxis (the ticket node) + the event log, not the context
  window. If compacted or re-spawned, reconstruct the working set from the pinned `as_of` view + the
  ticket's `meta.pinned_checks` + the log — losing the window should lose nothing.
- **Compact early, don't drop:** at **~50–60%** context fill, summarize old turns into a fixed compaction
  artifact: (1) end goal; (2) current approach; (3) steps completed; (4) **dead-ends tried and why they
  failed**; (5) key file locations + roles; (6) next step + its binary acceptance condition. Drop raw tool
  output, keep its conclusions.
- **Heartbeat across the gap:** before any long-running step, `heartbeat` the lease so the ticket doesn't
  go stale and get reclaimed mid-build.

## Decisions are episodes (the why, not just the what)

When the loop makes a non-obvious choice (picked library X; defaulted Y because the plan was silent),
record it with `praxis_record_episode` — `text` = decision + rationale, `alternatives` = options not taken.
Episodes are store-only and excluded from semantic recall by default, so the "why" compounds without
polluting task-grounding retrieval. Flip `outcome` later via `praxis_record_outcome` when the decision
proves out or fails.

## Deploy hard-gate

If the project declares a deploy/release step, it is a **hard gate, not advice**: deploy only after the
scoped build reaches completeness (every ticket `finished`) AND the WORK-review panel is satisfied (or
explicitly, recordedly skipped). A deploy whose preconditions are validation requirements covers them like
any other — external signal, recorded on the ticket, fail-closed.

## Never

- **Never** write or read any `.factory/*.json` manifest, build-status file, lock, or "awaiting subagents"
  flag — dynamic state lives ONLY on the Praxis ticket node; JSON is static config. Reaching for a JSON
  state file reintroduces the deleted bug.
- **Never** proceed when `_praxis` raises `PraxisUnreachable`, or cache/invent state to keep going. Fail
  closed: stop, surface the error.
- **Never** query the incomplete endpoint with the `prd-` prefix — pass the BARE project name, or it
  searches `prd-prd-<project>`, returns EMPTY, and fakes completeness.
- **Never** work more than one ticket at a time — pop ONE via `next_ready_ticket`, ship it end-to-end to
  `finished`, and only then look at the next. No batching, no surveying the queue, no pre-reading another
  ticket's requirements, no two tickets in context at once.
- **Never** claim a ticket that is not dependency-READY — its every `depends_on` prerequisite must be
  `finished`; a ticket waiting on an unfinished/in-progress job stays parked. If nothing is ready but work
  remains, that's a dependency stall — break it, don't spin.
- **Never** author or pre-bind a ticket's requirement list — which validation *requirements* apply is the
  fresh `resolve_validation_requirements` query at ticket start (truncate + re-derive); requirements are
  read-only during a build. You DO author the concrete *validations* that cover them — that is the point.
- **Never** pin a validation that does not faithfully cover a real requirement, and never finish with a
  non-empty `coverage_gap(cid)` — every retrieved requirement must be covered by a runnable validation.
- **Never** record a validation pass on the requirement fact — passes go on the TICKET NODE via
  `record_validation_pass`.
- **Never** skip verification — the build ALWAYS runs every pinned validation; verification is intrinsic.
- **Never** mark a ticket finished without `all_validations_passed(cid)` true (coverage complete + every
  validation green on an external signal, or human confirmation for non-coding); never fake a pass to escape.
- **Never** let an uncoverable/credential-only requirement wedge the run — `block(cid, owner, reason)` it so
  it is surfaced for owner action; a blocked ticket is excluded from churn, never silently passed or dropped.
- **Never** fake, stub, or weaken a pass to escape an **infra-dependent** requirement (Cognito against a
  real pool, the e2e login, a real-DB backfill, a federated relink) that cannot honestly go green locally —
  `block(cid, owner, reason)` it instead; the `build_completeness` gate completes AROUND the blocked ticket,
  so blocking never wedges the run.
- **Never** operate in a hardcoded `"agent-factory"` org or select around an org mismatch — run in the
  project-derived org (`PRAXIS_ORG` / `identity.factory_org()`); the hook-client org and the MCP-tool org
  (`whoami`/`select_org`) MUST agree, and a divergence is a fail-loud STOP to align, not to re-select past.
- **Never** stamp/clear the run marker for a scope you were not asked to build — the marked set IS the
  enforced run; ending it early (`clear_run`) with a marked ticket unfinished is an explicit abort, reported.
- **Never** trigger a correction from self-doubt alone — corrections require a failing signal; never use
  the generator's own model as the success judge; never accept an acceptance test written green.
- **Never** loop past the iteration cap / circuit breaker — escalate.
- **Never** run a crew **on a single ticket** — exactly ONE decision-making agent builds a given ticket.
  (Fanning the dependency-ready frontier out as parallel one-ticket workers via the ultracode Workflow is
  NOT a crew — it is deterministic scheduling, one decider per ticket; see *Execution model*.) Within a
  single ticket's build the only delegation is the disposable read-only retrieval sub-agent (it reads and
  digests, never decides/edits/writes/commits).
- **Never** start a new plan or add requirements here — af-build only finishes existing tickets (planning
  is af-plan; intake is af-intake-plan).
- **Never** build a ticket outside the requested scope; the parked non-scoped incomplete tickets MUST
  appear in the report — scoping is explicit, never a silent under-build.
- **Never** make the WORK-review advisory-only or self-reviewed; never pass on a missing ce panel (record
  no panel-ran episode, surface remediation); never skip the panel silently — every skip records a reason
  as a Praxis episode.
